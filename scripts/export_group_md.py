#!/usr/bin/env python3
"""从解密后的微信库导出指定群聊为 Markdown

用法:
    python export_group_md.py --dec "<沙盒>\\decrypted" --group "示例群名" --out "G:\\导出\\群聊.md"
    # 可选: --with-zstd (需已 pip install zstandard) 解压富文本消息
"""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
from datetime import datetime

# 群名/昵称常含 emoji；stdout 重定向到管道/文件时 Python 退回 GBK，替换而非崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
TYPE_MAP = {1: "文本", 3: "图片", 34: "语音", 43: "视频", 47: "表情", 48: "位置",
            49: "链接/文件", 51: "状态", 10000: "系统", 10002: "撤回", 266287972401: "拍一拍",
            244813135921: "复合"}
RICH_TYPES = set(range(49, 10000))  # 49 系复合类型按富文本处理

# 群消息内容普遍带 "<id>:\n" 前缀。坑：这个 id **未必等于发送者 wxid** ——
# 实测某群内容是 openim 业务号(1234567890@openim)，而 real_sender_id 映射的是另一个 wxid。
# 所以不能按发送者 wxid 精确匹配，改为按"看起来像 ID"剥（含 @ 或 wxid_ 前缀），
# 这样既覆盖业务号，又不会误伤"各位:\n"这类正常文本。
# ⚠️ 这个前缀不是噪音，而是【发信人】！群消息正文格式 = "<发信人username>:\n<内容>"
# （踩坑#20：早期版本把它当噪音剥掉，又用错误的 real_sender_id 映射补名字 -> 全员张冠李戴）
# 两种形态直接采信；老式微信号形态（纯字母数字）单独拆出来，必须命中已知用户名才剥，
# 否则 "Thanks:\n" 这类英文正文开头会被误当发信人吞掉（审计 E6）。
ID_PREFIX_RE = re.compile(
    r"^(wxid_[A-Za-z0-9_\-]+"                   # wxid_xxx
    r"|[A-Za-z0-9_.\-]+@[A-Za-z0-9_.\-]+"       # 业务号/群号: xxx@openim / xxx@chatroom
    r"):\r?\n")
LEGACY_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_\-]{5,19}):\r?\n")  # 老式微信号，需互证


def q(db, sql, args=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def find_file(dec, name):
    for root, _d, files in os.walk(dec):
        if name in files:
            return os.path.join(root, name)
    return None


def try_zstd(data):
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data).decode("utf-8", errors="replace")
    except ImportError:
        return None
    except Exception:
        return ""


def split_prefix(text, known_users):
    """剥「<发信人username>:\\n」前缀，返回 (发信人或None, 剩余正文)。
    wxid_/xxx@yyy 两种形态直接采信（足够特异）；老式微信号形态必须命中已知用户名集合，
    与本库 Name2Id/通讯录互证后才采信（审计 E6：防英文单词开头被误吞）。"""
    m = ID_PREFIX_RE.match(text)
    if m:
        return m.group(1), text[m.end():]
    m2 = LEGACY_PREFIX_RE.match(text)
    if m2 and m2.group(1) in known_users:
        return m2.group(1), text[m2.end():]
    return None, text


def rich_text_summary(xml_text):
    """从富文本 XML 提取可读摘要"""
    m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml_text, re.S)
    title = (m.group(1).strip() if m else "")
    m2 = re.search(r"<des>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</des>", xml_text, re.S)
    des = (m2.group(1).strip() if m2 else "")
    return title, des


def sqlite_out(path, room_id, gname, member_names, senders, nick, msgs):
    """写统计底座库：groups/members/messages 三张表。messages 整群重写（幂等、防重复导出翻倍，
    且不用 UNIQUE 去重——同秒同人同文的系统消息会误撞，实测静默丢 18 条，踩坑#21）。"""
    import sqlite3
    db = os.path.abspath(path)
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript("""
CREATE TABLE IF NOT EXISTS groups(
  room_id TEXT PRIMARY KEY, name TEXT, member_count INTEGER,
  msg_count INTEGER, first_time INTEGER, last_time INTEGER);
CREATE TABLE IF NOT EXISTS members(
  room_id TEXT, username TEXT, nickname TEXT, is_current INTEGER,
  PRIMARY KEY(room_id, username));
CREATE TABLE IF NOT EXISTS messages(
  room_id TEXT, ts INTEGER, sender TEXT, local_type INTEGER,
  is_system INTEGER, content TEXT);
CREATE INDEX IF NOT EXISTS idx_msg ON messages(room_id, ts);""")
    # 先把该群历史 is_current 全部清零再 upsert 当前名单：退群成员不会永远挂着 1（审计 E5）
    conn.execute("UPDATE members SET is_current=0 WHERE room_id=?", (room_id,))
    conn.executemany(
        "INSERT INTO members VALUES(?,?,?,?) ON CONFLICT(room_id,username) "
        "DO UPDATE SET nickname=excluded.nickname, is_current=excluded.is_current",
        [(room_id, m, nick.get(m, m), 1) for m in sorted(member_names)] +
        [(room_id, s, nick.get(s, s), 0) for s in sorted(senders) if s not in member_names])
    conn.execute("DELETE FROM messages WHERE room_id=?", (room_id,))
    conn.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?)", msgs)
    real = [m[1] for m in msgs if not m[4]]
    conn.execute("INSERT OR REPLACE INTO groups VALUES(?,?,?,?,?,?)",
                 (room_id, gname, len(member_names), len(real),
                  min(m[1] for m in msgs) if msgs else None,
                  max(m[1] for m in msgs) if msgs else None))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM messages WHERE room_id=?", (room_id,)).fetchone()[0]
    conn.close()
    print(f"结构化输出: {db}  （该群累计 {total} 条）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--group", help="群名关键词(匹配 nick_name；与 --username 二选一)")
    ap.add_argument("--username", help="群 username 精确定位(如 xxx@chatroom，上游已消歧时用)")
    ap.add_argument("--out", required=True, help="输出 Markdown 路径")
    ap.add_argument("--with-zstd", action="store_true", help="解压富文本(需 zstandard)")
    ap.add_argument("--sqlite", help="结构化输出 SQLite 路径（统计底座：群/成员/消息三张表）")
    ap.add_argument("--voice-map", help="语音时间线映射（export_voice.py 输出的 voice_map.json）："
                                        "把 [语音] 消息行嵌上对应 WAV 路径，可追溯到聊天时间")
    ap.add_argument("--media-map", help="图片时间线映射（export_media.py 输出的 media_map.json）："
                                        "把 [图片] 消息行嵌上解密后图片路径")
    ap.add_argument("--files-map", help="文件时间线映射（export_files.py 输出的 files_map.json）："
                                        "把文件消息行嵌上原文件路径")
    from media_common import add_time_args, parse_time_range
    add_time_args(ap)
    args = ap.parse_args()
    if not args.username and not args.group:
        sys.exit("[x] 需要 --group 群名关键词，或 --username 精确群 ID")

    voice_map = {}
    voice_root = None   # voice_map.json 所在 <导出根>/语音/，wav 路径相对 <导出根>
    if args.voice_map:
        import json as _json
        if not os.path.isfile(args.voice_map):
            sys.exit(f"[x] voice-map 文件不存在: {args.voice_map}")
        with open(args.voice_map, encoding="utf-8") as f:
            voice_map = _json.load(f)
        voice_root = os.path.dirname(os.path.dirname(os.path.abspath(args.voice_map)))
        print(f"[i] 语音时间线映射已加载: {sum(len(v) for v in voice_map.values())} 条")

    media_map, files_map = {}, {}
    media_root = files_root = None
    if args.media_map:
        import json as _json
        with open(args.media_map, encoding="utf-8") as f:
            media_map = _json.load(f)
        media_root = os.path.dirname(os.path.dirname(os.path.abspath(args.media_map)))
        print(f"[i] 图片时间线映射已加载: {sum(len(v) for v in media_map.values())} 张")
    if args.files_map:
        import json as _json
        with open(args.files_map, encoding="utf-8") as f:
            files_map = _json.load(f)
        files_root = os.path.dirname(os.path.dirname(os.path.abspath(args.files_map)))
        print(f"[i] 文件时间线映射已加载: {sum(len(v) for v in files_map.values())} 个")

    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)
    if since_ts or until_ts:
        print(f"[i] 时间过滤: {args.since or '不限'} ~ {args.until or '不限'} "
              f"({args.last or '全部'})")

    contact_db = find_file(args.dec, "contact.db")
    if not contact_db:
        sys.exit("[x] 找不到 contact.db")

    if args.username:            # 精确路径：上游 wx_export 已消歧，直接按 username 定位
        rows = q(contact_db, "SELECT username, nick_name, remark FROM contact WHERE username=?",
                 (args.username,))
        if not rows:
            sys.exit(f"[x] contact.db 中不存在 username={args.username}")
        g = rows[0]
    else:                        # 手动路径：与 wx_export.resolve_group 同语义 —— 不瞎猜
        kw = args.group.strip()
        pat = "%" + kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        # 只在 @chatroom 里找群：关键词撞上好友昵称时，绝不能把私聊当群导出（审计 E2）
        rows = q(contact_db,
                 "SELECT username, nick_name, remark FROM contact "
                 "WHERE username LIKE '%@chatroom' AND "
                 "(nick_name LIKE ? ESCAPE '\\' OR remark LIKE ? ESCAPE '\\')", (pat, pat))
        if not rows:
            sys.exit(f"[x] 未找到昵称/备注含「{args.group}」的群（只在 @chatroom 群里查找）")
        exact = [r for r in rows
                 if (r["nick_name"] or "") == kw or (r["remark"] or "") == kw]
        if len(exact) == 1:
            g = exact[0]
        elif len(rows) == 1:
            g = rows[0]
        else:
            for r in rows:
                print(f"候选: {r['nick_name']}  username={r['username']}")
            sys.exit(f"[x] 「{args.group}」命中 {len(rows)} 个群，请用完整群名或改用 --username，不瞎猜")
    uname, gname = g["username"], (g["nick_name"] or g["username"])
    md5 = hashlib.md5(uname.encode()).hexdigest()
    print(f"目标: {gname}  md5={md5}")

    # 定位 Msg 分表(遍历所有 message*.db)
    # ⚠️ 关键：一个群的消息可能被拆存到多个 message_N.db（实测：同一群在 message_0.db 与
    #    message_1.db 各存一段，时间完美衔接）。旧代码命中后不 break，导致"最后一个库覆盖前面的"，
    #    静默漏掉其余分表（实测漏 87%）。必须【全收集 + 合并】。
    found = []          # [(db路径, 表名, 条数)]
    for root, _d, files in os.walk(args.dec):
        for fn in sorted(files):
            if fn.startswith("message") and fn.endswith(".db"):
                db = os.path.join(root, fn)
                hits = [t["name"] for t in q(db, "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
                for t in hits:
                    if t.lower() == f"msg_{md5}":
                        c = q(db, f"SELECT COUNT(*) c FROM {t}")[0]["c"]
                        found.append((db, t, c))
    if not found:
        sys.exit(f"所有 message 库中都没有 Msg_{md5} 分表")
    print(f"定位: {len(found)} 个库含该群分表，合计 {sum(c for _d, _t, c in found)} 条")
    for db, t, c in found:
        print(f"   - {os.path.relpath(db, args.dec)} :: {t}  {c} 条")

    # username → 昵称（contact 表）；id2name 仅用于成员核验（member_id = 全局 name2id.rowid）
    # ⚠️ 绝不用 id2name 解析 real_sender_id（踩坑#20）：rid 局部于每个 message 库，
    #    全局 name2id 的 rowid 1~9 是系统账号(floatbottle/mphelper/weixin...)，小号撞车=张冠李戴
    id2name = {r["rid"]: r["username"] for r in q(contact_db, "SELECT rowid rid, username FROM name2id")}
    nick = {r["username"]: (r["remark"] or r["nick_name"] or r["username"])
            for r in q(contact_db, "SELECT username, nick_name, remark FROM contact")}
    # 群成员名单（当前名单）：成员核验与结构化输出共用。
    # chatroom_member.member_id = 全局 name2id.rowid（这个对应关系是对的，踩坑#20）
    member_names = set()
    if uname.endswith("@chatroom"):
        rr = q(contact_db, "SELECT rowid rid FROM name2id WHERE username=?", (uname,))
        if rr:
            member_names = {id2name[i] for i in
                            (r["member_id"] for r in q(contact_db,
                             "SELECT member_id FROM chatroom_member WHERE room_id=?", (rr[0]["rid"],)))
                            if i in id2name}

    # 合并多个库的行：不同库的 sort_seq 不具可比性，统一按 create_time 排序；
    # 跨库可能重复存同一条消息 -> 按 (时间, 发送者, 内容长度) 去重
    all_rows, seen = [], set()
    all_n2i_users = set()   # 全部库的 Name2Id 用户名并集（老式微信号前缀互证用，审计 E6）
    for db, t, _c in found:
        # 只与"前面的库"比对去重：同一库内的行本身就是不同记录，绝不能互删
        # （早期版本按 (时间,发送者,内容长度) 全库去重，会误删同秒同人的不同消息，实测少 1~4 条）
        # 本库 Name2Id = rid→username 权威映射（实测与前缀 21469 条 0 冲突；rid 局部于库）
        try:
            n2i = {r["rid"]: r["user_name"] for r in q(db, "SELECT rowid rid, user_name FROM Name2Id")}
        except Exception:
            n2i = {}
        all_n2i_users.update(n2i.values())
        cur = set()
        for r in q(db, f"SELECT * FROM {t}"):
            c = r["message_content"]
            if isinstance(c, bytes):
                ch = hashlib.md5(c).hexdigest()
            elif c is None:
                ch = ""
            else:
                ch = hashlib.md5(str(c).encode("utf-8", "replace")).hexdigest()
            key = (r["create_time"], r["real_sender_id"], ch)
            if key in seen:      # 前面的库已收录 -> 跨库重复，跳过
                continue
            cur.add(key)
            d = dict(r)
            d["local_u"] = n2i.get(r["real_sender_id"]) or ""
            all_rows.append(d)
        seen |= cur
    all_rows.sort(key=lambda r: (r["create_time"], r["sort_seq"] or 0))

    # ---- 完整性自检：把时间跨度打出来，让"漏数据"无处藏身（旧版静默失败，只显示条数看不出少了几个月）
    if all_rows:
        t0, t1 = all_rows[0]["create_time"], all_rows[-1]["create_time"]
        f0 = datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M")
        f1 = datetime.fromtimestamp(t1).strftime("%Y-%m-%d %H:%M")
        print(f"时间跨度: {f0}  ->  {f1}   ({(t1 - t0) / 86400 + 1:.0f} 天 / {len(all_rows)} 条)")
        lag_h = (datetime.now().timestamp() - t1) / 3600
        if lag_h > 48:
            print(f"  [!] 最新消息距今 {lag_h:.0f} 小时（>2天）—— 若该群近期有发言，"
                  f"可能是分库未合并或本地未同步，请核对")

    # ---- 发信人解析（踩坑#20，两级定案）：
    #  1) 消息内容前缀 "<username>:\n" = 发信人真身（他人消息必带前缀）
    #  2) 无前缀消息 = 自己发的（微信不给自己加前缀）-> 用【消息所在库】的 Name2Id 解析
    #     （自己 rid 跨库不同属正常：每个库的 rid 序号空间独立）

    # wcdb_builtin_compression_record: 记录哪些列被 WCDB 压缩(4=calendar) — 本导出只按 WCDB_CT_* 判定
    stats = {"total": 0, "plain": 0, "zstd_ok": 0, "zstd_skip": 0}
    lines = [f"# {gname} — 聊天记录导出\n",
             f"> 导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    cur_day = None
    senders = set()      # 供成员核验/成员表用
    msgs = []            # 结构化输出缓冲: (room_id, ts, sender, local_type, is_system, content)
    known_users = set(nick) | all_n2i_users   # 老式微信号前缀互证集合（审计 E6）
    for r in all_rows:
        ts = r["create_time"]
        if since_ts is not None and ts < since_ts:
            continue
        if until_ts is not None and ts > until_ts:
            continue
        stats["total"] += 1
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if day != cur_day:
            cur_day = day
            lines.append(f"\n## {day}\n")
        t = datetime.fromtimestamp(ts).strftime("%H:%M")
        mt = r["local_type"]
        label = TYPE_MAP.get(mt, "富文本" if mt in RICH_TYPES
                             else "名片" if mt == 42 else
                             "应用消息" if mt > 100000 else "微信消息")
        content = r["message_content"]
        text = None
        sender_u = None      # 内容前缀里提取到的发信人（真身）
        if isinstance(content, bytes) and content.startswith(ZSTD_MAGIC):
            if args.with_zstd:
                dec = try_zstd(content)
                if dec is None:
                    stats["zstd_skip"] += 1
                    text = f"[{label}·需zstandard]"
                else:
                    stats["zstd_ok"] += 1
                    dec = dec.replace("\r\n", "\n")   # 内容里是 \r\n，不归一化的话前缀/XML 判定全失效
                    # 前缀 = 发信人（踩坑#20）：先取出再剥掉；不剥的话富文本会整坨 XML 吐出来
                    sender_u, dec = split_prefix(dec, known_users)
                    text = dec
            else:
                stats["zstd_skip"] += 1
                text = f"[{label}·压缩未解]"
        elif isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
            stats["plain"] += 1
        elif content:
            text = str(content)
            stats["plain"] += 1
        else:
            text = f"[{label}]"
        # 归一化 + 前缀提取（明文分支在这里取到发信人）
        if isinstance(text, str):
            text = text.replace("\r\n", "\n")
            p, text = split_prefix(text, known_users)
            sender_u = sender_u or p
            # 明文存储的富文本 XML 同样走摘要（审计 E8：不依赖"富文本必被压缩"的实测规律）
            if text.lstrip().startswith("<"):
                # 语音消息正文 = voicemsg 元数据 XML（zstd 压缩）：只提取时长，别把整段 XML 倒进 Markdown。
                # 三种结构：① length=毫秒（精确）；② 仅 voicelength=字节（SILK 约 1KB/s 估算，标 ~）；
                #   ③ voice_map 命中时用 WAV 头实测。任一带 <voicemsg 都不得泄漏 XML。
                is_voice = bool(re.search(r"<voicemsg", text))
                vm = re.search(r"<voicemsg[^>]*\blength\s*=\s*\"(\d+)\"", text)
                if vm:
                    sec = int(vm.group(1)) / 1000
                    text = f"[语音 {sec:.0f}s]" if sec >= 1 else "[语音]"
                elif is_voice and voice_root and v is not None and v.get("wav"):
                    wav_p = os.path.join(voice_root, v["wav"])
                    if os.path.isfile(wav_p):
                        try:
                            with open(wav_p, "rb") as wf:
                                wf.seek(40)
                                dsz = int.from_bytes(wf.read(4), "little")
                            text = f"[语音 {dsz / 48000:.0f}s]"
                        except Exception:
                            text = "[语音]"
                    else:
                        text = "[语音]"
                elif is_voice:
                    vl = re.search(r"<voicemsg[^>]*voicelength\s*=\s*\"(\d+)\"", text)
                    if vl:
                        sec = int(vl.group(1)) / 1000
                        text = f"[语音 ~{sec:.0f}s]" if sec >= 1 else "[语音]"
                    else:
                        text = "[语音]"
                else:
                    title, des = rich_text_summary(text)
                    if title or des:
                        text = f"[{label}] " + " | ".join(x for x in (title, des) if x)
        text = (text or "").replace("\r", "").strip()
        # 发信人：内容前缀（真身）> 本库 Name2Id。绝不用全局 name2id 兜底（小号雷区，踩坑#20）
        u = sender_u or r["local_u"]
        who = nick.get(u, u) if u else f"未知ID_{r['real_sender_id']}"
        if u and mt not in (10000, 10002) and mt != 266287972401:
            senders.add(u)      # 系统消息/拍一拍无发信人，不进统计
        msgs.append((uname, ts, u or "", mt,
                     1 if (mt in (10000, 10002) or mt == 266287972401) else 0, text))
        if mt in (10000, 10002) or mt == 266287972401:
            lines.append(f"- `{t}` 系统: {text[:160]}")
        else:
            # 语音消息嵌入 WAV 路径（--voice-map）：同一时间源 create_time，可直接对应聊天时间
            v = None
            if mt == 34 and voice_map:
                v = voice_map.get(md5, {}).get(str(r.get("server_id") or ""))
                if v:
                    text = f"{text} 🎤 {v['wav']}" if text != "[语音]" else f"[语音] 🎤 {v['wav']}"
            lines.append(f"- `{t}` **{who}**: {text}")

    # ---- 完整性自检2：发信人应属于本群成员名单（防"张冠李戴"复发，踩坑#20）
    if senders and member_names:
        inside = sum(1 for s in senders if s in member_names)
        print(f"成员核验: 发送者 {len(senders)} 人中 {inside} 人在本群成员名单({len(member_names)}人)，"
              f"名单外 {len(senders) - inside} 人（多为已退群成员，属正常）")
        if inside < len(senders) * 0.5:
            print("  [!] 过半发信人不在成员名单 -> 发信人映射可能仍有问题(踩坑#20)")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # ---- 结构化输出（统计底座）：与 Markdown 同源同一次解析，字段语义见《数据结构说明.md》
    if args.sqlite:
        sqlite_out(args.sqlite, uname, gname, member_names, senders, nick, msgs)

    n_sys = sum(1 for m in msgs if m[4])
    print(f"\n统计: {stats}")
    print(f"消息口径对账: 本次共 {len(msgs)} 条 = 有效 {len(msgs) - n_sys} 条"
          f"（统计底座 msg_count / 群刊「有效消息」同源采用此数）+ 系统/撤回/拍一拍 {n_sys} 条")
    print(f"输出: {args.out}")


if __name__ == "__main__":
    main()
