#!/usr/bin/env python3
"""export_all_sessions.py — 跨全部会话按时间批量导出（v2.4 新增）

场景：「帮我梳理一下昨天所有聊天记录，总结一下有哪些事项」——需要跨全部会话
（群 @chatroom + 私聊 wxid）一次性按时间窗捞消息，而不是一次只导一个群/会话。

【架构：从消息出发，SQL 时间窗直查，会话只做命名】（与 export_group_md「单群定位」相反）：
  1. 只遍历 message/message_<N>.db（N 为数字 0..8；biz_message/media_*/message_fts 等同前缀库一律排除）。
  2. 每个分库只开【一次】连接：一次性 SELECT name FROM sqlite_master 拿到该库全部 Msg_ 表名，
     一次性读出该库 Name2Id（rid→user_name，局部于库）。
  3. 对每张 Msg_<md5(username)> 表直接跑带时间窗的 SQL：
        SELECT ... FROM "Msg_<hash>" WHERE create_time >= ? AND create_time <= ?
     昨天/近窗没消息的会话 SQL 自然 0 命中，根本不进结果——不逐个会话探测。
  4. 命中消息按「表名 hash」分组、组内按 (create_time, sort_seq) 排序；跨库分片沿用
     export_group_md 已验证的 (时间,发送者,内容哈希) 去重（只与前面的库比）。
  5. 最后一次性 hash→username→昵称：contact.db 一次查 username/nick_name/remark 建字典，
     再 md5(username)→username 反查表名 hash；SessionTable 仅作「枚举会话总数/跳过清单」
     与真实会话 universe，绝不用于驱动查询。

发信人解析沿用 export_group_md 两级定案（踩坑#20）：内容前缀 "<发信人>:\n"（群内他人消息真身）
> 本库 Name2Id 解析 real_sender_id（无前缀=自己发，或私聊对方）。

用法:
    python export_all_sessions.py --dec "<decrypted>" --last 昨天 --out "%TEMP%\\汇总.md"
    python export_all_sessions.py --dec "<decrypted>" --last 7d --out 汇总.md --sqlite 底座.db --json 底座.json
    python export_all_sessions.py --dec "<decrypted>" --last all --out 全量.md --include-private   # 只要私聊
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

# 复用 export_group_md 的已验证读取/前缀/解压工具（同目录脚本，import 不触发其 main）
from export_group_md import (ZSTD_MAGIC, TYPE_MAP, RICH_TYPES, find_file, q,
                             split_prefix, try_zstd, rich_text_summary)
from media_common import add_time_args, parse_time_range

# 真实消息分库：仅 message_0..8.db（N 为数字）。biz_message_0.db / media_*.db /
# message_fts.db / message_resource.db / weclaw.db 一律不扫（实测：它们或无 Msg_ 表，或属公众号/索引）。
MESSAGE_DB_RE = re.compile(r"^message_\d+\.db$")
# 系统/撤回/拍一拍 local_type（整值哨兵，不取低位——与 chat_stats 一致）
SYSTEM_LOCAL_TYPES = {10000, 10002, 266287972401}


def is_system_type(local_type):
    return (local_type or 0) in SYSTEM_LOCAL_TYPES


def display_content(text, local_type, label):
    """把原始文本归一化并收口：XML 富文本/语音只留摘要，不把整段 XML 倒进输出（审计 E8）。"""
    if not isinstance(text, str):
        return text
    text = text.replace("\r\n", "\n").replace("\r", "")
    # 语音消息正文 = voicemsg 元数据 XML：只提取时长
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
    # 明文/解压后的富文本 XML：提 title/des 摘要
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
      enumerated: set(username)   （SessionTable 真实会话 universe，用于"枚举会话总数/跳过"统计）
    命名字典(nick/hash2user)覆盖 contact 全部 username + SessionTable；但【枚举 universe 只取
    SessionTable 真实有会话记录的 username】——contact 里 2.6 万条多为从未聊过的陌生人/公众号，
    全算进"枚举会话"会把这个数字虚高到失真（踩坑：早期版本 26303 vs 真实会话 1454）。"""
    contact_db = find_file(dec, "contact.db")
    if not contact_db:
        sys.exit("[x] 找不到 contact.db")
    nick = {}
    hash2user = {}
    for r in q(contact_db, "SELECT username, nick_name, remark FROM contact WHERE username != ''"):
        u = r["username"]
        nick[u] = r["remark"] or r["nick_name"] or u
        hash2user[hashlib.md5(u.encode()).hexdigest()] = u

    enumerated = set()
    session_db = find_file(dec, "session.db")
    if session_db:
        try:
            for r in q(session_db, "SELECT username FROM SessionTable WHERE username != ''"):
                u = r["username"]
                enumerated.add(u)          # 真实会话 universe（仅这里计入"枚举总数"）
                h = hashlib.md5(u.encode()).hexdigest()
                if h not in hash2user:
                    hash2user[h] = u
                    nick.setdefault(u, u)
        except Exception as e:
            print(f"[!] 读 session.db SessionTable 失败({e})，回退用 contact 表做枚举 universe")
            enumerated = set(nick)
    else:
        enumerated = set(nick)             # 没有 session.db 时退化：用 contact 命名表兜底
    return nick, hash2user, enumerated


def collect_messages(dec, since_ts, until_ts, include_groups, include_private,
                     with_zstd, max_text):
    """核心：逐分库一次连接扫描，每张 Msg_ 表一次时间窗 SQL。
    返回:
      sessions: {username: [msg, ...]}  （msg=dict: ts, sort_seq, sender_u, sender_disp,
                                          local_type, is_system, content）
      skipped_hashes: set(table hash)   （命中窗口但按 include 过滤掉的会话，归入"跳过/过滤"）
      db_stats: {库名: 表数/行数}
    """
    nick, hash2user, enumerated = load_contact_maps(dec)
    all_n2i_users = set()     # 全部库 Name2Id 用户名并集（老式微信号前缀互证用，审计 E6）
    sessions = {}             # username -> [msg dict]
    skipped_hashes = set()
    seen = set()              # 跨库去重 key=(create_time, real_sender_id, 内容哈希)
    db_stats = {}

    message_dbs = sorted(
        db for db in glob.glob(os.path.join(dec, "message", "*.db"))
        if MESSAGE_DB_RE.match(os.path.basename(db)))
    if not message_dbs:
        sys.exit(f"[x] 在 {dec}\\message 下没找到 message_<N>.db 分库")

    for db in message_dbs:
        fn = os.path.basename(db)
        # 本库只开一次连接：列全部 Msg_ 表 + 读 Name2Id
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        try:
            n2i = {r["rid"]: r["user_name"] for r in
                   conn.execute("SELECT rowid rid, user_name FROM Name2Id")}
        except Exception:
            n2i = {}
        all_n2i_users.update(n2i.values())
        n_tbl = n_hit = n_row = 0

        for t in tables:
            n_tbl += 1
            h = t[4:].lower()                      # 去 "Msg_" 前缀得会话 hash
            uname = hash2user.get(h)
            # 命名/过滤：hash 反查不到 username 的会话无法判群/私聊，默认放行（标"未知会话"）
            is_group = (uname or "").endswith("@chatroom")
            if is_group and not include_groups:
                skipped_hashes.add(h)
                continue
            if (not is_group) and not include_private:
                skipped_hashes.add(h)
                continue

            # 时间窗直查：无命中即空，不进结果
            sql = (f'SELECT local_type, real_sender_id, create_time, sort_seq, message_content '
                   f'FROM [{t}] WHERE create_time >= ? AND create_time <= ?')
            try:
                rows = conn.execute(sql, (since_ts, until_ts)).fetchall()
            except Exception as e:
                print(f"  [x] {fn}::{t} 查询失败: {e}")
                continue
            if not rows:
                continue
            n_hit += 1
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
                if key in seen:                # 前面的库已收录 -> 跨库重复，跳过
                    continue
                cur.add(key)
                n_row += 1

                ts = r["create_time"]
                mt = r["local_type"] or 0
                label = type_label(mt)
                sender_u = None
                text = None
                # zstd 压缩富文本：解开才能拿内容前缀（发信人真身）
                if isinstance(content, bytes) and content.startswith(ZSTD_MAGIC):
                    if with_zstd:
                        dec_t = try_zstd(content)
                        if dec_t:
                            dec_t = dec_t.replace("\r\n", "\n")
                            sender_u, dec_t = split_prefix(dec_t, set(nick) | all_n2i_users)
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
                # 明文分支也剥前缀（群内他人消息真身）
                if isinstance(text, str):
                    text = text.replace("\r\n", "\n")
                    p, text = split_prefix(text, set(nick) | all_n2i_users)
                    sender_u = sender_u or p
                    text = display_content(text, mt, label)
                text = (text or "").strip()
                if max_text and len(text) > max_text:
                    text = text[:max_text] + "…"
                # 发信人：内容前缀 > 本库 Name2Id（绝不用全局 name2id 兜底，踩坑#20）
                local_u = n2i.get(r["real_sender_id"]) or ""
                u = sender_u or local_u
                who = nick.get(u, u) if u else f"ID_{r['real_sender_id']}"
                sessions.setdefault(uname or f"<未知会话:{h}>", []).append({
                    "ts": ts,
                    "sort_seq": r["sort_seq"] or 0,
                    "sender_u": u or "",
                    "sender_disp": who,
                    "local_type": mt,
                    "is_system": is_system_type(mt),
                    "content": text,
                })
            seen |= cur
        db_stats[fn] = (n_tbl, n_hit, n_row)
        conn.close()

    # 组内按时间正序
    for u in sessions:
        sessions[u].sort(key=lambda m: (m["ts"], m["sort_seq"]))
    return sessions, nick, hash2user, enumerated, skipped_hashes, db_stats


def write_markdown(path, sessions, nick, since_ts, until_ts,
                   enumerated_n, hit_n, skipped_n, total_msgs, sys_msgs):
    """写汇总 Markdown：开头统计块，每个会话一节，组内按时间正序。"""
    lines = ["# 全部会话聊天记录汇总\n",
             f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    if since_ts or until_ts:
        f0 = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M") if since_ts else "不限"
        f1 = datetime.fromtimestamp(until_ts).strftime("%Y-%m-%d %H:%M") if until_ts else "不限"
        lines.append(f"> 时间范围: {f0} ~ {f1}\n")
    lines.append(f"> 枚举会话总数: {enumerated_n}　|　时间窗内有消息会话: {hit_n}　"
                 f"|　跳过(无命中/被过滤): {skipped_n}")
    lines.append(f"> 消息总数: {total_msgs}（有效 {total_msgs - sys_msgs} / 系统·撤回·拍一拍 {sys_msgs}）\n")

    # 会话按条数降序（最活跃的排前，便于抓重点）；条数相同按首条时间升序
    ordered = sorted(sessions.items(),
                     key=lambda kv: (-len(kv[1]), kv[1][0]["ts"]))
    for uname, msgs in ordered:
        is_group = uname.endswith("@chatroom")
        kind = "群" if is_group else ("私聊" if not uname.startswith("<") else "未知会话")
        disp = nick.get(uname, uname)
        first = datetime.fromtimestamp(msgs[0]["ts"]).strftime("%H:%M")
        last = datetime.fromtimestamp(msgs[-1]["ts"]).strftime("%H:%M")
        lines.append(f"\n## {disp}（{kind} · {len(msgs)} 条 · {first}~{last}）\n")
        lines.append(f"- username: `{uname}`")
        for m in msgs:
            t = datetime.fromtimestamp(m["ts"]).strftime("%H:%M")
            if m["is_system"]:
                lines.append(f"  - `{t}` 系统: {m['content']}")
            else:
                lines.append(f"  - `{t}` **{m['sender_disp']}**: {m['content']}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_sqlite(path, sessions, nick):
    """结构化底座：sessions + messages 两表，供上层 AI 总结事项。"""
    db = os.path.abspath(path)
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript("""
CREATE TABLE IF NOT EXISTS sessions(
  username TEXT PRIMARY KEY, display_name TEXT, kind TEXT,
  msg_count INTEGER, first_time INTEGER, last_time INTEGER);
CREATE TABLE IF NOT EXISTS messages(
  session_username TEXT, create_time INTEGER, sender TEXT, sender_display TEXT,
  local_type INTEGER, is_system INTEGER, content TEXT);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_username, create_time);""")
    srows, mrows = [], []
    for uname, msgs in sessions.items():
        kind = "群" if uname.endswith("@chatroom") else ("私聊" if not uname.startswith("<") else "未知")
        srows.append((uname, nick.get(uname, uname), kind, len(msgs),
                      msgs[0]["ts"], msgs[-1]["ts"]))
        for m in msgs:
            mrows.append((uname, m["ts"], m["sender_u"], m["sender_disp"],
                          m["local_type"], 1 if m["is_system"] else 0, m["content"]))
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM messages")
    conn.executemany("INSERT INTO sessions VALUES(?,?,?,?,?,?)", srows)
    conn.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", mrows)
    conn.commit()
    conn.close()
    print(f"结构化底座(SQLite): {db}  （{len(srows)} 会话 / {len(mrows)} 消息）")


def write_json(path, sessions, nick, since_ts, until_ts):
    """JSON 结构化底座（与 SQLite 同源，供上层程序读）。"""
    data = {
        "meta": {
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "since_ts": since_ts, "until_ts": until_ts,
            "session_count": len(sessions),
            "message_count": sum(len(v) for v in sessions.values()),
        },
        "sessions": [],
    }
    for uname, msgs in sorted(sessions.items(), key=lambda kv: -len(kv[1])):
        data["sessions"].append({
            "username": uname,
            "display_name": nick.get(uname, uname),
            "kind": "群" if uname.endswith("@chatroom") else ("私聊" if not uname.startswith("<") else "未知"),
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


def main():
    ap = argparse.ArgumentParser(description="跨全部会话按时间批量导出（v2.4）")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--out", required=True, help="输出汇总 Markdown 路径")
    ap.add_argument("--sqlite", help="可选：结构化 SQLite 底座路径（sessions/messages 两表）")
    ap.add_argument("--json", help="可选：结构化 JSON 底座路径")
    ap.add_argument("--include-groups", dest="include_groups", action="store_true", default=True,
                    help="包含群聊（默认开）")
    ap.add_argument("--no-groups", dest="include_groups", action="store_false",
                    help="排除群聊")
    ap.add_argument("--include-private", dest="include_private", action="store_true", default=True,
                    help="包含私聊（默认开）")
    ap.add_argument("--no-private", dest="include_private", action="store_false",
                    help="排除私聊")
    ap.add_argument("--max-text", type=int, default=300,
                    help="单条正文截断字数（默认 300，防爆体积；0=不截断）")
    ap.add_argument("--no-zstd", action="store_true", help="跳过 zstd 解压（快，压缩消息显示占位）")
    add_time_args(ap)
    args = ap.parse_args()

    # 无时间过滤时给个提示（全量可能很大）
    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)
    if since_ts is None and until_ts is None:
        print("[!] 未指定时间窗（--last/--since/--until）：将拉取全部历史消息，体积可能很大。")
    else:
        f0 = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M") if since_ts else "不限"
        f1 = datetime.fromtimestamp(until_ts).strftime("%Y-%m-%d %H:%M") if until_ts else "不限"
        print(f"[i] 时间过滤: {f0} ~ {f1}  ({args.last or '自定义'})")
    print(f"[i] 范围: 群={'开' if args.include_groups else '关'}  "
          f"私聊={'开' if args.include_private else '关'}  单条截断={args.max_text or 0}字")

    with_zstd = not args.no_zstd
    t0 = _time.time()
    sessions, nick, hash2user, enumerated, skipped_hashes, db_stats = collect_messages(
        args.dec, since_ts, until_ts, args.include_groups, args.include_private,
        with_zstd, args.max_text)
    elapsed = _time.time() - t0

    # 统计
    total_msgs = sum(len(v) for v in sessions.values())
    sys_msgs = sum(1 for v in sessions.values() for m in v if m["is_system"])
    enumerated_n = len(enumerated)
    hit_n = len(sessions)
    # 跳过 = 枚举 universe 里没命中的 + 被 include 过滤掉的表
    hit_usernames = set(sessions.keys())
    skipped_sessions = enumerated - hit_usernames
    skipped_n = len(skipped_sessions) + len(skipped_hashes)

    print("\n" + "=" * 56)
    print("逐分库扫描统计（库: 表数/命中表/命中行）:")
    for fn, (nt, nh, nr) in sorted(db_stats.items()):
        print(f"  {fn:16s} 表 {nt:4d} / 命中表 {nh:4d} / 命中行 {nr:5d}")
    print("=" * 56)
    print(f"枚举会话总数: {enumerated_n}")
    print(f"时间窗内有消息会话: {hit_n}")
    print(f"跳过会话数: {skipped_n}（其中被群/私聊开关过滤 {len(skipped_hashes)} 表；"
          f"其余 {len(skipped_sessions)} 个枚举会话在本时间窗无消息）")
    if skipped_sessions:
        sample = sorted(skipped_sessions)[:5]
        print(f"  跳过样例(前5, 脱敏只显示是否群): "
              f"{[('群' if u.endswith('@chatroom') else '私聊') for u in sample]}")
    print(f"消息总数: {total_msgs}（有效 {total_msgs - sys_msgs} / 系统 {sys_msgs}）")
    print(f"耗时: {elapsed:.1f}s")

    if total_msgs == 0:
        sys.exit("[x] 时间窗内没有任何消息（检查 --last/--since/--until）")

    write_markdown(args.out, sessions, nick, since_ts, until_ts,
                   enumerated_n, hit_n, skipped_n, total_msgs, sys_msgs)
    print(f"\n汇总 Markdown: {args.out}")
    if args.sqlite:
        write_sqlite(args.sqlite, sessions, nick)
    if args.json:
        write_json(args.json, sessions, nick, since_ts, until_ts)


if __name__ == "__main__":
    main()
