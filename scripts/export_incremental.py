#!/usr/bin/env python3
"""增量导出聊天记录 —— 记录上次导出位置，只导出新增消息

思路（参考 wechat-cli 的 last_check.json）:
    维护一个状态文件（默认 <outdir>/.wechat_export_state.json），
    记录每个会话已导出到的最大 create_time / local_id。
    首次运行即全量导出并初始化状态；后续运行只导出"上次之后"的新消息，
    追加到 Markdown 文件末尾，跑完更新状态。

用法:
    # 首次运行（自动全量 + 初始化状态）
    python export_incremental.py --dec "<dec>" --session "群名" --out "导出/群名.md"

    # 后续运行（只导出新增）
    python export_incremental.py --dec "<dec>" --session "群名" --out "导出/群名.md"

    # 忽略状态，强制全量重导
    python export_incremental.py --dec "<dec>" --session "群名" --out "导出/群名.md" --full

    # 指定状态文件路径
    python export_incremental.py --dec "<dec>" --session "群名" --out "导出/群名.md" --state "导出/.state.json"
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

# stdout 重定向到管道/文件时 Python 退回 GBK，替换而非崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# 复用 export_group_md.py 的常量与工具函数（同目录 import）
from export_group_md import (
    ZSTD_MAGIC, TYPE_MAP, RICH_TYPES,
    find_file, q, try_zstd, split_prefix, rich_text_summary,
)
from media_common import add_time_args, parse_time_range


def load_state(state_path):
    """加载增量状态文件，返回 dict。文件不存在返回空 dict。"""
    if os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print(f"[!] 状态文件损坏或不可读，将视为首次运行: {state_path}")
    return {"version": 1, "sessions": {}}


def save_state(state_path, state):
    """保存增量状态文件。"""
    os.makedirs(os.path.dirname(os.path.abspath(state_path)), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def resolve_session(contact_db, group_kw=None, username=None):
    """解析会话为 (username, display_name)。复用 export_group_md.py 的消歧逻辑。"""
    if username:
        rows = q(contact_db, "SELECT username, nick_name, remark FROM contact WHERE username=?",
                 (username,))
        if not rows:
            sys.exit(f"[x] contact.db 中不存在 username={username}")
        g = rows[0]
    else:
        kw = group_kw.strip()
        pat = "%" + kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = q(contact_db,
                 "SELECT username, nick_name, remark FROM contact "
                 "WHERE (username LIKE ? ESCAPE '\\' OR nick_name LIKE ? ESCAPE '\\' OR remark LIKE ? ESCAPE '\\')",
                 (pat, pat, pat))
        if not rows:
            sys.exit(f"[x] 未找到含「{kw}」的联系人/群")
        exact = [r for r in rows if (r["nick_name"] or "") == kw or (r["remark"] or "") == kw]
        if len(exact) == 1:
            g = exact[0]
        elif len(rows) == 1:
            g = rows[0]
        else:
            for r in rows:
                print(f"候选: {r['nick_name'] or r['remark']}  username={r['username']}")
            sys.exit(f"[x] 「{kw}」命中 {len(rows)} 个，请用完整名称或 --username")
    return g["username"], (g["nick_name"] or g["remark"] or g["username"])


def find_msg_tables(dec, md5):
    """遍历所有 message_*.db，找含 Msg_<md5> 分表的库。返回 [(db路径, 表名)]"""
    found = []
    for root, _d, files in os.walk(dec):
        for fn in sorted(files):
            if fn.startswith("message") and fn.endswith(".db") and "fts" not in fn:
                db = os.path.join(root, fn)
                hits = [t["name"] for t in q(db,
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
                for t in hits:
                    if t.lower() == f"msg_{md5}":
                        found.append((db, t))
    return found


def read_messages_incremental(dec, md5, nick, known_users, last_ts=None, with_zstd=False):
    """读取指定会话的消息，可按 last_ts 过滤（只取 create_time > last_ts 的消息）。

    复用 export_group_md.py 的读取范式：遍历多个 message_*.db 的 Msg_<md5> 表，
    跨库合并去重，按时间排序。
    """
    found = find_msg_tables(dec, md5)
    if not found:
        return [], 0, 0

    all_rows, seen = [], set()
    all_n2i_users = set()
    for db, t in found:
        try:
            n2i = {r["rid"]: r["user_name"] for r in
                   q(db, "SELECT rowid rid, user_name FROM Name2Id")}
        except Exception:
            n2i = {}
        all_n2i_users.update(n2i.values())
        # 增量模式下加 WHERE 条件过滤（减少读取量）
        if last_ts is not None:
            rows = q(db, f"SELECT * FROM {t} WHERE create_time > ?", (last_ts,))
        else:
            rows = q(db, f"SELECT * FROM {t}")
        cur = set()
        for r in rows:
            c = r["message_content"]
            if isinstance(c, bytes):
                ch = hashlib.md5(c).hexdigest()
            elif c is None:
                ch = ""
            else:
                ch = hashlib.md5(str(c).encode("utf-8", "replace")).hexdigest()
            key = (r["create_time"], r["real_sender_id"], ch)
            if key in seen:
                continue
            cur.add(key)
            d = dict(r)
            d["local_u"] = n2i.get(r["real_sender_id"]) or ""
            all_rows.append(d)
        seen |= cur
    all_rows.sort(key=lambda r: (r["create_time"], r["sort_seq"] or 0))
    return all_rows, len(found), len(all_n2i_users)


def format_message(r, nick, known_users, with_zstd=False):
    """把一条原始消息行格式化为 Markdown 行。复用 export_group_md.py 的解析逻辑。"""
    mt = r["local_type"]
    label = TYPE_MAP.get(mt, "富文本" if mt in RICH_TYPES
                         else "名片" if mt == 42 else
                         "应用消息" if mt > 100000 else "微信消息")
    content = r["message_content"]
    text = None
    sender_u = None

    if isinstance(content, bytes) and content.startswith(ZSTD_MAGIC):
        if with_zstd:
            dec = try_zstd(content)
            if dec is None:
                text = f"[{label}·需zstandard]"
            else:
                dec = dec.replace("\r\n", "\n")
                sender_u, dec = split_prefix(dec, known_users)
                text = dec
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
        p, text = split_prefix(text, known_users)
        sender_u = sender_u or p
        if text.lstrip().startswith("<"):
            is_voice = bool(re.search(r"<voicemsg", text))
            vm = re.search(r"<voicemsg[^>]*\blength\s*=\s*\"(\d+)\"", text)
            if vm:
                sec = int(vm.group(1)) / 1000
                text = f"[语音 {sec:.0f}s]" if sec >= 1 else "[语音]"
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
    u = sender_u or r["local_u"]
    who = nick.get(u, u) if u else f"未知ID_{r['real_sender_id']}"
    is_system = mt in (10000, 10002) or mt == 266287972401
    return who, text, is_system


def main():
    ap = argparse.ArgumentParser(description="增量导出聊天记录（记录上次导出位置，只导出新增）")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--session", help="会话名关键词（群名/联系人名，与 --username 二选一）")
    ap.add_argument("--username", help="会话 username 精确定位（如 xxx@chatroom）")
    ap.add_argument("--out", required=True, help="输出 Markdown 路径（增量追加到此文件）")
    ap.add_argument("--state", help="状态文件路径（默认 <outdir>/.wechat_export_state.json）")
    ap.add_argument("--full", action="store_true", help="忽略状态文件，强制全量重导")
    ap.add_argument("--with-zstd", action="store_true", help="解压富文本(需 zstandard)")
    from media_common import add_time_args, parse_time_range
    add_time_args(ap)
    args = ap.parse_args()
    if not args.username and not args.session:
        sys.exit("[x] 需要 --session 会话名关键词，或 --username 精确 ID")

    # 状态文件默认位置：输出文件同级目录下
    if args.state:
        state_path = args.state
    else:
        out_dir = os.path.dirname(os.path.abspath(args.out))
        state_path = os.path.join(out_dir, ".wechat_export_state.json")

    state = load_state(state_path)
    print(f"[i] 状态文件: {state_path}")

    contact_db = find_file(args.dec, "contact.db")
    if not contact_db:
        sys.exit("[x] 找不到 contact.db")

    # ---- 解析会话 ----
    uname, disp_name = resolve_session(contact_db, args.session, args.username)
    md5 = hashlib.md5(uname.encode()).hexdigest()
    print(f"[i] 目标会话: {disp_name}  username={uname}  md5={md5}")

    # 加载昵称映射
    nick = {r["username"]: (r["remark"] or r["nick_name"] or r["username"])
            for r in q(contact_db, "SELECT username, nick_name, remark FROM contact")}

    # ---- 确定增量起点 ----
    session_state = state.get("sessions", {}).get(uname, {})
    last_ts = session_state.get("last_create_time")
    is_first_run = not session_state or args.full

    if is_first_run:
        if args.full:
            print("[i] --full 模式：忽略状态文件，全量重导")
        else:
            print("[i] 首次运行（无状态）：全量导出并初始化状态")
        last_ts = None  # 全量
    else:
        print(f"[i] 增量模式：只导出 create_time > {last_ts} "
              f"({datetime.fromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M')}) 的新消息")

    # ---- 读取消息 ----
    found_tables = find_msg_tables(args.dec, md5)
    if not found_tables:
        sys.exit(f"[x] 所有 message 库中都没有 Msg_{md5} 分表")
    print(f"[i] 定位 {len(found_tables)} 个库含该会话分表")

    all_rows, n_tables, n_users = read_messages_incremental(
        args.dec, md5, nick, set(nick) | set(),
        last_ts=last_ts, with_zstd=args.with_zstd)

    if not all_rows:
        print(f"[i] 无新增消息（上次导出后没有新内容）")
        # 即使无新增也更新状态的时间戳
        if not args.full and session_state:
            state["sessions"][uname]["last_check"] = datetime.now().isoformat()
            save_state(state_path, state)
        return

    print(f"[i] 读取到 {len(all_rows)} 条消息")

    # 时间范围过滤
    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)
    filtered = []
    for r in all_rows:
        ts = r["create_time"]
        if since_ts is not None and ts < since_ts:
            continue
        if until_ts is not None and ts > until_ts:
            continue
        filtered.append(r)
    all_rows = filtered

    if not all_rows:
        print(f"[i] 时间过滤后无消息")
        return

    print(f"[i] 时间过滤后 {len(all_rows)} 条")

    # ---- 格式化输出 ----
    lines = []
    cur_day = None
    new_max_ts = all_rows[-1]["create_time"]  # 已按时间升序
    new_max_local_id = max(r.get("local_id", 0) for r in all_rows)

    if is_first_run:
        # 全量导出：写完整头部
        lines.append(f"# {disp_name} — 聊天记录导出（全量）\n")
        lines.append(f"> 导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    else:
        # 增量追加：加分隔线
        lines.append(f"\n---\n")
        lines.append(f"# {disp_name} — 增量更新\n")
        lines.append(f"> 导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}  "
                     f"| 新增 {len(all_rows)} 条\n")

    known_users = set(nick)
    for r in all_rows:
        ts = r["create_time"]
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if day != cur_day:
            cur_day = day
            lines.append(f"\n## {day}\n")
        t = datetime.fromtimestamp(ts).strftime("%H:%M")
        who, text, is_system = format_message(r, nick, known_users, with_zstd=args.with_zstd)
        if is_system:
            lines.append(f"- `{t}` 系统: {text[:160]}")
        else:
            lines.append(f"- `{t}` **{who}**: {text}")

    # ---- 写入文件 ----
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if is_first_run:
        # 全量模式：覆盖写
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    else:
        # 增量模式：追加
        with open(args.out, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))

    # ---- 更新状态 ----
    if "sessions" not in state:
        state["sessions"] = {}
    state["sessions"][uname] = {
        "last_create_time": new_max_ts,
        "last_local_id": new_max_local_id,
        "export_count": session_state.get("export_count", 0) + len(all_rows),
        "last_exported": datetime.now().isoformat(),
        "display_name": disp_name,
    }
    save_state(state_path, state)

    print(f"\n[✓] 本次导出 {len(all_rows)} 条消息")
    print(f"[✓] 最新消息时间: {datetime.fromtimestamp(new_max_ts).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[✓] 累计导出: {state['sessions'][uname]['export_count']} 条")
    print(f"[✓] 输出文件: {args.out}")
    print(f"[✓] 状态已更新: {state_path}")


if __name__ == "__main__":
    main()
