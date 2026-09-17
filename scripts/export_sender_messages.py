#!/usr/bin/env python3
"""export_sender_messages.py — 按发送者精确直查（v2.7 新增）

场景: 只要某个发送者（通常是自己）发的消息——做个人发言画像 / 单方审计 / 发言统计，
      别全量导出再在结果里筛。SQL 层直接 WHERE real_sender_id IN (rids) 精准取数。

【为什么快】
  全量导出（export_all_sessions）拉的是"全部发送者"的消息，量级是目标方的几倍到几十倍；
  本脚本在 SQL 层就按发送者 rid 过滤，单表命中即直出，不做全量中转再筛。
  本机实测: 全库约 142 万行、跨 11 个 message_<N>.db、1475 张 Msg_ 表，
  直查本人 142,434 条消息仅约 5s；随后交叉校验误配率约 0.015%（见 --verify）。

【发送者身份的三条不变量（务必理解，否则会导错人）】
  1. rid 每库各自为政: 同一 wxid 在 message_0..8.db 的 Name2Id 里 rowid 可能不同，
     必须【逐库】读该库 Name2Id 解析目标 wxid 的 rid，绝不可拿 A 库的 rid 去 B 库过滤。
  2. 内容前缀 > 本库 Name2Id: 群内他人消息的真身是内容前缀 "<发信人>:\n"；目标发送者
     自己发的消息理论上【不应该】带他人前缀。出现前缀 = 疑似误配，记入 --verify 报告。
  3. 跨库去重: 同一消息可能被多个分库重复收录，按 (create_time, real_sender_id, 内容哈希)
     与前面库比对去重（与 export_all_sessions 同一套键，实测有效）。

用法:
    python export_sender_messages.py --dec "<decrypted>" --sender <本人wxid> --out 我的发言.db
    python export_sender_messages.py --dec "<decrypted>" --sender <wxid> --last 7d --out 近7天.md
    python export_sender_messages.py --dec "<decrypted>" --sender <wxid> --session "项目群" --out 项目群.db
    python export_sender_messages.py --dec "<decrypted>" --sender <wxid> --verify --out 校验.json
"""
import argparse
import glob
import hashlib
import json as _json
import os
import re
import sqlite3
import sys
import time as _time
from datetime import datetime

# 群名/昵称常含 emoji；stdout 重定向到管道/文件时 Python 退回 GBK，替换而非崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# 复用同目录已验证的读取/前缀/解压/时间窗工具（import 不触发其 main）
from export_group_md import (ZSTD_MAGIC, TYPE_MAP, RICH_TYPES, find_file, q,
                             split_prefix, try_zstd, rich_text_summary)
from media_common import add_time_args, parse_time_range

# 真实消息分库：仅 message_0..8.db（N 为数字）。biz_message_0.db / media_*.db /
# message_fts.db / message_resource.db / weclaw.db 一律不扫（与 export_all_sessions 一致）
MESSAGE_DB_RE = re.compile(r"^message_\d+\.db$")
# 系统/撤回/拍一拍 local_type（整值哨兵，不取低位）
SYSTEM_LOCAL_TYPES = {10000, 10002, 266287972401}


def is_system_type(local_type):
    return (local_type or 0) in SYSTEM_LOCAL_TYPES


def display_content(text, local_type, label):
    """把原始文本归一化并收口：XML 富文本/语音只留摘要，不把整段 XML 倒进输出。"""
    if not isinstance(text, str):
        return text
    text = text.replace("\r\n", "\n").replace("\r", "")
    is_voice = bool(re.search(r"<voicemsg", text))
    if is_voice:
        vm = re.search(r'<voicemsg[^>]*\blength\s*=\s*"(\d+)"', text)
        if vm:
            sec = int(vm.group(1)) / 1000
            return f"[语音 {sec:.0f}s]" if sec >= 1 else "[语音]"
        vl = re.search(r'<voicemsg[^>]*voicelength\s*=\s*"(\d+)"', text)
        if vl:
            sec = int(vl.group(1)) / 1000
            return f"[语音 ~{sec:.0f}s]" if sec >= 1 else "[语音]"
        return "[语音]"
    if text.lstrip().startswith("<"):
        title, des = rich_text_summary(text)
        if title or des:
            return f"[{label}] " + " | ".join(x for x in (title, des) if x)
    return text.strip()


def type_label(local_type):
    """local_type -> 中文类型标签（系统/撤回整值哨兵；其余整值或按 49 系富文本）。"""
    if local_type in TYPE_MAP:
        return TYPE_MAP[local_type]
    if local_type in RICH_TYPES:
        return "富文本"
    if local_type == 42:
        return "名片"
    if local_type and local_type > 100000:
        return "应用消息"
    return "微信消息"


def load_contact_maps(dec):
    """一次性建两张字典（只读 contact.db / session.db 各一次连接）：
      nick: username -> 显示名(备注>昵称>username)
      hash2user: md5(username).hexdigest() -> username   （Msg_ 表名 hash 反查）
    与 export_all_sessions.load_contact_maps 同构（枚举 universe 不需要，只取命名）。"""
    contact_db = find_file(dec, "contact.db")
    if not contact_db:
        sys.exit("[x] 找不到 contact.db")
    nick = {}
    hash2user = {}
    for r in q(contact_db, "SELECT username, nick_name, remark FROM contact WHERE username != ''"):
        u = r["username"]
        nick[u] = r["remark"] or r["nick_name"] or u
        hash2user[hashlib.md5(u.encode()).hexdigest()] = u
    session_db = find_file(dec, "session.db")
    if session_db:
        try:
            for r in q(session_db, "SELECT username FROM SessionTable WHERE username != ''"):
                u = r["username"]
                h = hashlib.md5(u.encode()).hexdigest()
                if h not in hash2user:
                    hash2user[h] = u
                    nick.setdefault(u, u)
        except Exception as e:
            print(f"[!] 读 session.db SessionTable 失败({e})，回退用 contact 表")
    return nick, hash2user


def resolve_session_hash(hash2user, nick, session_arg):
    """把 --session 显示名片段解析为 (username, table_hash)；唯一匹配，多匹配报错。"""
    hits = [u for u in nick if session_arg in nick[u] or session_arg in u]
    if not hits:
        sys.exit(f"[x] 会话 '{session_arg}' 无匹配。用 --list-contacts 看可用的显示名。")
    if len(hits) > 1:
        names = " / ".join(sorted(nick[u] for u in hits)[:8])
        sys.exit(f"[x] 会话 '{session_arg}' 匹配到多个: {names}。请用更精确的片段。")
    u = hits[0]
    return u, hashlib.md5(u.encode()).hexdigest()


def collect_sender_messages(dec, senders, since_ts, until_ts,
                            session_hash, with_zstd, max_text, verify):
    """核心：逐分库一次连接，逐 Msg_ 表按发送者 rid + 时间窗直查。

    返回:
      sessions: {username: [msg, ...]}   （msg dict: ts, sort_seq, sender_u, sender_disp,
                                          local_type, is_system, content）
      stats:    {库名: (表数, 命中表, 命中行)}
      verify_rows: [疑似误配消息摘要, ...]（--verify 时收集，否则 []）
      sender_disp: 目标 wxid 的显示名（None 表示未知联系人）
    """
    nick, hash2user = load_contact_maps(dec)
    sender_set = set(senders)
    nick_set = set(nick)   # 缓存一次：split_prefix 需 known_users 集合，2.6 万元素重建很贵
    verify_rows = []

    message_dbs = sorted(
        db for db in glob.glob(os.path.join(dec, "message", "*.db"))
        if MESSAGE_DB_RE.match(os.path.basename(db)))
    if not message_dbs:
        sys.exit(f"[x] 在 {dec}\\message 下没找到 message_<N>.db 分库")

    sessions = {}
    seen = set()          # 跨库去重 key=(create_time, real_sender_id, 内容哈希)
    db_stats = {}
    sender_disp = None

    for db in message_dbs:
        fn = os.path.basename(db)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        # 不变量#1：rid 每库各自为政 —— 本库 Name2Id 解析目标 wxid 的 rid
        my_rids = set()
        try:
            for r in conn.execute("SELECT rowid rid, user_name FROM Name2Id"):
                if r["user_name"] in sender_set:
                    my_rids.add(r["rid"])
        except Exception:
            pass
        n_tbl = n_hit = n_row = 0

        for t in tables:
            n_tbl += 1
            if session_hash and t[4:].lower() != session_hash:
                continue
            if not my_rids:
                break  # 本库无目标 rid，余下表格也不会命中
            sql = (f'SELECT local_type, real_sender_id, create_time, sort_seq, message_content '
                   f'FROM [{t}] WHERE real_sender_id IN ({",".join("?" for _ in my_rids)})')
            args = list(my_rids)
            if since_ts is not None:
                sql += " AND create_time >= ?"
                args.append(since_ts)
            if until_ts is not None:
                sql += " AND create_time <= ?"
                args.append(until_ts)
            try:
                rows = conn.execute(sql, args).fetchall()
            except Exception as e:
                print(f"  [x] {fn}::{t} 查询失败: {e}")
                continue
            if not rows:
                continue
            n_hit += 1
            h = t[4:].lower()
            uname = hash2user.get(h) or f"<未知会话:{h}>"
            cur = set()
            for r in rows:
                content = r["message_content"]
                if isinstance(content, bytes):
                    ch = hashlib.md5(content).hexdigest()
                elif content is None:
                    ch = ""
                else:
                    ch = hashlib.md5(str(content).encode("utf-8", "replace")).hexdigest()
                key = (r["create_time"], r["real_sender_id"], ch)
                if key in seen:            # 前面的库已收录 -> 跨库重复，跳过
                    continue
                cur.add(key)
                n_row += 1

                ts = r["create_time"]
                mt = r["local_type"] or 0
                label = type_label(mt)
                text = None
                prefix_sender = None
                if isinstance(content, bytes) and content.startswith(ZSTD_MAGIC):
                    if with_zstd:
                        dec_t = try_zstd(content)
                        if dec_t:
                            dec_t = dec_t.replace("\r\n", "\n")
                            prefix_sender, dec_t = split_prefix(dec_t, nick_set)
                            text = dec_t
                        else:
                            text = f"[{label}·压缩未解]"
                    else:
                        text = f"[{label}·压缩未解]"
                elif isinstance(content, bytes):
                    text = content.decode("utf-8", errors="replace")
                elif content:
                    text = str(content)
                else:
                    text = f"[{label}]"
                if isinstance(text, str):
                    text = text.replace("\r\n", "\n")
                    p, text = split_prefix(text, nick_set)
                    prefix_sender = prefix_sender or p
                    text = display_content(text, mt, label)
                text = (text or "").strip()
                if max_text and len(text) > max_text:
                    text = text[:max_text] + "…"

                # 不变量#2：目标发送者自己的消息不该带他人前缀；带了 = 疑似误配
                if verify and prefix_sender:
                    verify_rows.append({
                        "ts": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                        "session": nick.get(uname, uname),
                        "prefix": prefix_sender,
                        "content": text[:120],
                    })

                sessions.setdefault(uname, []).append({
                    "ts": ts,
                    "sort_seq": r["sort_seq"] or 0,
                    "sender_u": senders[0],
                    "sender_disp": next((u for u in senders if u in nick), ""),
                    "local_type": mt,
                    "is_system": is_system_type(mt),
                    "content": text,
                })
            seen |= cur
        db_stats[fn] = (n_tbl, n_hit, n_row)
        conn.close()

    # 目标发送者显示名（任一 wxid 在通讯录里有备注/昵称即取）
    for s in senders:
        if s in nick:
            sender_disp = nick[s]
            break

    for u in sessions:
        sessions[u].sort(key=lambda m: (m["ts"], m["sort_seq"]))
    return sessions, db_stats, verify_rows, sender_disp


def write_sqlite(path, sessions, nick, senders, sender_disp, verify_rows, since_ts, until_ts):
    """结构化底座：sessions + messages 两表 + meta（口径/校验），供上层做统计/画像。"""
    db = os.path.abspath(path)
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript("""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  session_username TEXT PRIMARY KEY, display_name TEXT, kind TEXT,
  msg_count INTEGER, first_time INTEGER, last_time INTEGER);
CREATE TABLE IF NOT EXISTS messages(
  session_username TEXT, create_time INTEGER, sender TEXT, sender_display TEXT,
  local_type INTEGER, is_system INTEGER, content TEXT);
CREATE TABLE IF NOT EXISTS verify_suspicious(
  ts TEXT, session TEXT, prefix TEXT, content TEXT);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_username, create_time);""")
    conn.execute("DELETE FROM meta")
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM messages")
    meta = {
        "senders": ",".join(senders),
        "sender_display": sender_disp or "",
        "total_msgs": sum(len(v) for v in sessions.values()),
        "verify_suspicious": str(len(verify_rows)),
        "export_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "since_ts": str(since_ts or ""),
        "until_ts": str(until_ts or ""),
    }
    conn.executemany("INSERT INTO meta VALUES(?,?)", list(meta.items()))
    srows, mrows = [], []
    for uname, msgs in sessions.items():
        kind = "群" if uname.endswith("@chatroom") else "私聊"
        srows.append((uname, nick.get(uname, uname), kind, len(msgs),
                      msgs[0]["ts"], msgs[-1]["ts"]))
        for m in msgs:
            mrows.append((uname, m["ts"], m["sender_u"], m["sender_disp"],
                          m["local_type"], 1 if m["is_system"] else 0, m["content"]))
    conn.executemany("INSERT INTO sessions VALUES(?,?,?,?,?,?)", srows)
    conn.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", mrows)
    if verify_rows:
        conn.executemany("INSERT INTO verify_suspicious VALUES(?,?,?,?)",
                         [(v["ts"], v["session"], v["prefix"], v["content"]) for v in verify_rows])
    conn.commit()
    conn.close()
    print(f"结构化底座(SQLite): {db}  （{len(srows)} 会话 / {len(mrows)} 消息）")


def write_json(path, sessions, nick, senders, sender_disp, verify_rows, since_ts, until_ts):
    data = {
        "meta": {
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "senders": senders,
            "sender_display": sender_disp,
            "since_ts": since_ts, "until_ts": until_ts,
            "session_count": len(sessions),
            "message_count": sum(len(v) for v in sessions.values()),
            "verify_suspicious": len(verify_rows),
        },
        "verify_rows": verify_rows,
        "sessions": [],
    }
    for uname, msgs in sorted(sessions.items(), key=lambda kv: -len(kv[1])):
        data["sessions"].append({
            "username": uname,
            "display_name": nick.get(uname, uname),
            "kind": "群" if uname.endswith("@chatroom") else "私聊",
            "msg_count": len(msgs),
            "first_time": msgs[0]["ts"], "last_time": msgs[-1]["ts"],
            "messages": [{"ts": m["ts"], "sender": m["sender_u"], "sender_display": m["sender_disp"],
                          "local_type": m["local_type"], "is_system": m["is_system"],
                          "content": m["content"]} for m in msgs],
        })
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"结构化底座(JSON): {path}  （{len(data['sessions'])} 会话 / "
          f"{data['meta']['message_count']} 消息）")


def write_markdown(path, sessions, nick, senders, sender_disp, since_ts, until_ts):
    """写 Markdown：开头统计块，每个会话一节，组内按时间正序。"""
    lines = ["# 按发送者直查：聊天记录汇总\n",
             f"> 目标发送者: {sender_disp or senders[0]}（{' / '.join(senders)}）\n",
             f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    if since_ts or until_ts:
        f0 = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M") if since_ts else "不限"
        f1 = datetime.fromtimestamp(until_ts).strftime("%Y-%m-%d %H:%M") if until_ts else "不限"
        lines.append(f"> 时间范围: {f0} ~ {f1}\n")
    total = sum(len(v) for v in sessions.values())
    lines.append(f"> 命中会话: {len(sessions)}　|　消息总数: {total}\n")
    for uname, msgs in sorted(sessions.items(), key=lambda kv: -len(kv[1])):
        kind = "群" if uname.endswith("@chatroom") else "私聊"
        disp = nick.get(uname, uname)
        first = datetime.fromtimestamp(msgs[0]["ts"]).strftime("%H:%M")
        last = datetime.fromtimestamp(msgs[-1]["ts"]).strftime("%H:%M")
        lines.append(f"\n## {disp}（{kind} · {len(msgs)} 条 · {first}~{last}）\n")
        for m in msgs:
            t = datetime.fromtimestamp(m["ts"]).strftime("%H:%M")
            if m["is_system"]:
                lines.append(f"  - `{t}` 系统: {m['content']}")
            else:
                lines.append(f"  - `{t}` **{m['sender_disp'] or m['sender_u']}**: {m['content']}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"汇总 Markdown: {path}")


def main():
    ap = argparse.ArgumentParser(description="按发送者精确直查（v2.7）")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--sender", required=True, action="append",
                    help="目标发送者 wxid（可重复，逐库按 Name2Id 解析 rid 直查）")
    ap.add_argument("--session", help="可选：只查该会话（显示名片段，唯一匹配）")
    ap.add_argument("--out", required=True, help="输出路径：.db→SQLite底座 / .json→JSON / .md→Markdown")
    ap.add_argument("--max-text", type=int, default=300,
                    help="单条正文截断字数（默认 300；0=不截断）")
    ap.add_argument("--no-zstd", action="store_true", help="跳过 zstd 解压（快，压缩消息显示占位）")
    ap.add_argument("--verify", action="store_true",
                    help="交叉校验：收集带他人前缀的消息（疑似误配）写入报告")
    add_time_args(ap)
    args = ap.parse_args()

    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)
    if since_ts is None and until_ts is None:
        print("[!] 未指定时间窗（--last/--since/--until）：将拉取目标发送者全部历史消息。")
    with_zstd = not args.no_zstd
    print(f"[i] 目标发送者: {' / '.join(args.sender)}  校验={'开' if args.verify else '关'}  "
          f"单条截断={args.max_text or 0}字")

    nick, hash2user = load_contact_maps(args.dec)
    session_hash = None
    if args.session:
        uname, session_hash = resolve_session_hash(hash2user, nick, args.session)
        print(f"[i] 会话限定: {nick.get(uname, uname)}")

    t0 = _time.time()
    sessions, db_stats, verify_rows, sender_disp = collect_sender_messages(
        args.dec, args.sender, since_ts, until_ts, session_hash, with_zstd, args.max_text, args.verify)
    elapsed = _time.time() - t0

    total = sum(len(v) for v in sessions.values())
    print("\n" + "=" * 56)
    print("逐分库扫描统计（库: 表数/命中表/命中行）:")
    for fn, (nt, nh, nr) in sorted(db_stats.items()):
        print(f"  {fn:16s} 表 {nt:4d} / 命中表 {nh:4d} / 命中行 {nr:5d}")
    print("=" * 56)
    print(f"命中会话: {len(sessions)}")
    print(f"消息总数: {total}（有效 {total - sum(1 for v in sessions.values() for m in v if m['is_system'])} / "
          f"系统·撤回·拍一拍 {sum(1 for v in sessions.values() for m in v if m['is_system'])}）")
    if verify_rows:
        print(f"[!] 疑似误配(带他人前缀): {len(verify_rows)} 条，详见输出文件 verify_rows 段")
    print(f"耗时: {elapsed:.1f}s")

    if total == 0:
        sys.exit("[x] 时间窗内该发送者没有任何消息（检查 wxid / 时间范围）")

    ext = os.path.splitext(args.out)[1].lower()
    if ext == ".db":
        write_sqlite(args.out, sessions, nick, args.sender, sender_disp,
                     verify_rows, since_ts, until_ts)
    elif ext == ".json":
        write_json(args.out, sessions, nick, args.sender, sender_disp,
                   verify_rows, since_ts, until_ts)
    else:
        write_markdown(args.out, sessions, nick, args.sender, sender_disp, since_ts, until_ts)


if __name__ == "__main__":
    main()
