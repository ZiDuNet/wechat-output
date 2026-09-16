#!/usr/bin/env python3
"""chat_stats.py — 群聊 / 私聊统计分析台（v2.4 新增）

对指定一个群或联系人，基于已解密库输出 Markdown 统计报告：
    - 消息总数（含/不含系统消息两个口径）
    - 按消息类型分布表（文本/图片/语音/视频/表情/链接文件/系统...）
    - 按发送人发言条数排行 Top N（系统消息/拍一拍无发信人，不参与排行）
    - 按小时活跃分布（0~23 点 ASCII 直方图）
    - 按日活跃分布（活跃天数 / 日均 / 峰值日）
    - 时间跨度（首条~末条，含首尾覆盖天）

底层读 message_*.db 的 Msg_<md5> 分表，发信人解析复用 export_group_md 的两级定案
（内容前缀 > 本库 Name2Id），时间过滤走 media_common。纯增量脚本，不改现有任何脚本。

用法:
    python chat_stats.py --dec "<decrypted>" --session "群名或联系人"
    python chat_stats.py --dec "<decrypted>" --username "xxx@chatroom" --last 30d --top 15
    python chat_stats.py --dec "<decrypted>" --session "晓东" --since 2026-08-01 --until 2026-09-01
    python chat_stats.py --dec "<decrypted>" --session "群名" --no-zstd   # 跳过 zstd 解压（快，发信人用本库 Name2Id）
"""
import argparse
import hashlib
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime

# 群名/昵称常含 emoji；stdout 重定向到管道/文件时 Python 退回 GBK，替换而非崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# 复用 export_group_md 的读取/前缀/解压工具（同目录脚本，import 不触发其 main）
from export_group_md import (ZSTD_MAGIC, find_file, q, split_prefix, try_zstd)
from media_common import add_time_args, parse_time_range

# local_type 低位 (local_type & 255) → 中文标签（微信 4.x 类型表）
LOW_TYPE_LABEL = {
    1: "文本", 3: "图片", 34: "语音", 43: "视频", 47: "表情",
    48: "位置", 49: "链接/文件/小程序", 50: "通话",
    10000: "系统", 10002: "撤回",
}
# 带高位 flag 的复合类型（拍一拍/复合）按整值特判，避免低位落到无意义桶
SPECIAL_TYPE_LABEL = {266287972401: "拍一拍", 244813135921: "复合"}
SYSTEM_LOCAL_TYPES = {10000, 10002, 266287972401}


def type_label(local_type):
    """local_type -> 中文类型标签。
    注意：10000/10002 是整值哨兵（不取低位——10000&0xFF=16 会错）；
    拍一拍/复合按整值特判；其余按低位 (local_type & 0xFF) 归类（appmsg 的 flag 大数也归到 49）。"""
    if local_type == 10000:
        return "系统"
    if local_type == 10002:
        return "撤回"
    if local_type in SPECIAL_TYPE_LABEL:
        return SPECIAL_TYPE_LABEL[local_type]
    return LOW_TYPE_LABEL.get((local_type or 0) & 0xFF, f"其他({local_type})")


def is_system_type(local_type):
    return (local_type or 0) in SYSTEM_LOCAL_TYPES


def resolve_session(dec, session_arg):
    """把 --session 解析为 (username, display_name, is_group)。

    与 wx_export/export_group_md 同语义：
      1) 看起来像 username（含 @ / wxid_ / filehelper 等）→ 直接查 contact
      2) 否则当群名关键词，只在 @chatroom 群里模糊查；没命中再放宽到联系人（私聊）
    命中多个候选时取精确匹配，否则取第一个（与现有脚本一致，不瞎猜多取）。
    """
    contact_db = find_file(dec, "contact.db")
    if not contact_db:
        sys.exit("[x] 找不到 contact.db")
    arg = session_arg.strip()

    # 策略1: 直接当 username
    if "@" in arg or arg.startswith("wxid_") or arg in ("filehelper", "weixin", "fmessage"):
        rows = q(contact_db,
                 "SELECT username, nick_name, remark FROM contact WHERE username=?", (arg,))
        if rows:
            r = rows[0]
            return (r["username"],
                    r["nick_name"] or r["remark"] or r["username"],
                    r["username"].endswith("@chatroom"))
        sys.exit(f"[x] username「{arg}」在 contact.db 中不存在")

    pat = "%" + arg.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    # 策略2: 先在群里找
    rows = q(contact_db,
             "SELECT username, nick_name, remark FROM contact "
             "WHERE username LIKE '%@chatroom' AND "
             "(nick_name LIKE ? ESCAPE '\\' OR remark LIKE ? ESCAPE '\\')", (pat, pat))
    scope = "群"
    if not rows:
        # 再放宽到联系人（私聊）
        rows = q(contact_db,
                 "SELECT username, nick_name, remark FROM contact "
                 "WHERE username LIKE ? ESCAPE '\\' OR nick_name LIKE ? ESCAPE '\\' "
                 "OR remark LIKE ? ESCAPE '\\'", (pat, pat, pat))
        scope = "联系人"
        if not rows:
            sys.exit(f"[x] --session「{arg}」在通讯录中未找到（群与联系人都没有）")
    exact = [r for r in rows
             if (r["nick_name"] or "") == arg or (r["remark"] or "") == arg]
    target = exact[0] if len(exact) == 1 else rows[0]
    if len(rows) > 1 and len(exact) != 1:
        print(f"[!] 「{arg}」命中 {len(rows)} 个{scope}，选用第一个: "
              f"{target['nick_name'] or target['remark'] or target['username']}")
    return (target["username"],
            target["nick_name"] or target["remark"] or target["username"],
            target["username"].endswith("@chatroom"))


def load_messages(dec, username, since_ts=None, until_ts=None, with_zstd=True, content_cap=200):
    """加载指定会话全部消息（跨 message_*.db 合并 + 跨库去重），返回 (msgs, nick, db_hits)。

    msgs: [{ts, sender, sender_disp, local_type, is_system, content}]
    发信人两级定案（踩坑#20）：内容前缀 "<id>:\n"（他人消息真身）> 本库 Name2Id。
    content 截断到 content_cap 字，供素材包/摘录用；统计只用 ts/sender/local_type。
    """
    contact_db = find_file(dec, "contact.db")
    nick = {r["username"]: (r["remark"] or r["nick_name"] or r["username"])
            for r in q(contact_db, "SELECT username, nick_name, remark FROM contact")}
    md5 = hashlib.md5(username.encode()).hexdigest()

    # 定位所有含该分表的 message 库（踩坑#19：必须全收集合并，不能只取一个）
    found = []
    for root, _d, files in os.walk(dec):
        for fn in sorted(files):
            if fn.startswith("message") and fn.endswith(".db"):
                db = os.path.join(root, fn)
                for (t,) in q(db, "SELECT name FROM sqlite_master "
                                  "WHERE type='table' AND name LIKE 'Msg_%'"):
                    if t.lower() == f"msg_{md5}":
                        found.append((db, t))
    if not found:
        return [], nick, 0

    all_n2i_users = set()
    all_rows = []
    seen = set()
    for db, t in found:
        # 本库 Name2Id：rid→user_name（局部于库，踩坑#20）
        try:
            n2i = {r["rid"]: r["user_name"]
                   for r in q(db, "SELECT rowid rid, user_name FROM Name2Id")}
        except Exception:
            n2i = {}
        all_n2i_users.update(n2i.values())
        cur = set()
        for r in q(db, f"SELECT local_type, real_sender_id, create_time, message_content "
                       f"FROM [{t}]"):
            content = r["message_content"]
            if isinstance(content, bytes):
                ch = hashlib.md5(content).hexdigest()
            elif content is None:
                ch = ""
            else:
                ch = hashlib.md5(str(content).encode("utf-8", "replace")).hexdigest()
            key = (r["create_time"], r["real_sender_id"], ch)
            if key in seen:        # 跨库重复（只与前面的库比），跳过
                continue
            cur.add(key)
            all_rows.append((r, n2i))
        seen |= cur

    known_users = set(nick) | all_n2i_users
    msgs = []
    for r, n2i in all_rows:
        ts = r["create_time"]
        if ts is None:
            continue
        if since_ts is not None and ts < since_ts:
            continue
        if until_ts is not None and ts > until_ts:
            continue
        mt = r["local_type"] or 0
        content = r["message_content"]
        sender_u = None
        text = ""
        # zstd 压缩富文本：解开才能拿到内容前缀（发信人真身）
        if isinstance(content, bytes) and content.startswith(ZSTD_MAGIC) and with_zstd:
            dec_t = try_zstd(content)
            if dec_t:
                dec_t = dec_t.replace("\r\n", "\n")
                sender_u, dec_t = split_prefix(dec_t, known_users)
                text = dec_t
        elif isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        elif content:
            text = str(content)
        # 明文分支也剥前缀
        if isinstance(text, str):
            text = text.replace("\r\n", "\n")
            p, text = split_prefix(text, known_users)
            sender_u = sender_u or p
        local_u = n2i.get(r["real_sender_id"]) or ""
        u = sender_u or local_u
        who = nick.get(u, u) if u else f"ID_{r['real_sender_id']}"
        msgs.append({
            "ts": ts,
            "sender": u,
            "sender_disp": who,
            "local_type": mt,
            "is_system": is_system_type(mt),
            "content": (text or "").replace("\r", " ").strip()[:content_cap],
        })
    msgs.sort(key=lambda m: m["ts"])
    return msgs, nick, len(found)


def compute_stats(msgs, top_n=15):
    """从 msgs 列表计算统计结构。"""
    total = len(msgs)
    type_c = Counter()
    sender_c = Counter()
    hourly = [0] * 24
    daily = Counter()
    for m in msgs:
        type_c[type_label(m["local_type"])] += 1
        if not m["is_system"]:
            sender_c[m["sender_disp"]] += 1
        dt = datetime.fromtimestamp(m["ts"])
        hourly[dt.hour] += 1
        daily[dt.strftime("%Y-%m-%d")] += 1
    sys_n = sum(1 for m in msgs if m["is_system"])
    span = None
    if msgs:
        span = (msgs[0]["ts"], msgs[-1]["ts"])
    active_days = len(daily)
    avg_per_day = round(total / active_days, 1) if active_days else 0
    peak_day = daily.most_common(1)[0] if daily else None
    return {
        "total": total,
        "effective": total - sys_n,
        "system": sys_n,
        "type_dist": type_c.most_common(),
        "top_senders": sender_c.most_common(top_n),
        "hourly": hourly,
        "daily": sorted(daily.items()),
        "active_days": active_days,
        "avg_per_day": avg_per_day,
        "peak_day": peak_day,
        "span": span,
    }


def _barh(count, max_count, width=24):
    return "#" * int(count / max_count * width) if max_count else ""


def render_report(name, stats, db_hits, since_ts=None, until_ts=None):
    """把统计结构渲染成 Markdown 报告。"""
    span = stats["span"]
    lines = [f"# {name} — 聊天统计报告\n",
             f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    if since_ts or until_ts:
        lines.append(f"> 时间范围: "
                     f"{datetime.fromtimestamp(since_ts).strftime('%Y-%m-%d') if since_ts else '不限'}"
                     f" ~ "
                     f"{datetime.fromtimestamp(until_ts).strftime('%Y-%m-%d') if until_ts else '不限'}\n")
    if span:
        f0 = datetime.fromtimestamp(span[0]).strftime("%Y-%m-%d %H:%M")
        f1 = datetime.fromtimestamp(span[1]).strftime("%Y-%m-%d %H:%M")
        cover_days = int((span[1] - span[0]) / 86400) + 1
        lines.append(f"> 时间跨度: {f0}  →  {f1}  （含首尾 {cover_days} 天，{db_hits} 个分库）\n")

    lines.append("\n## 消息总数\n")
    lines.append(f"- **总消息数**: {stats['total']}")
    lines.append(f"- 有效消息（不含系统/撤回/拍一拍）: {stats['effective']}")
    lines.append(f"- 系统/撤回/拍一拍: {stats['system']}\n")

    lines.append("\n## 类型分布\n")
    lines.append("| 类型 | 条数 | 占比 |")
    lines.append("|---|---|---|")
    for label, cnt in stats["type_dist"]:
        pct = cnt / stats["total"] * 100 if stats["total"] else 0
        lines.append(f"| {label} | {cnt} | {pct:.1f}% |")

    lines.append("\n## 发言排行（Top {0}，不含系统消息）\n".format(len(stats["top_senders"])))
    if stats["top_senders"]:
        lines.append("| 排名 | 发送人 | 条数 | 占有效消息 |")
        lines.append("|---|---|---|---|")
        eff = stats["effective"] or 1
        for i, (who, cnt) in enumerate(stats["top_senders"], 1):
            lines.append(f"| {i} | {who} | {cnt} | {cnt/eff*100:.1f}% |")
    else:
        lines.append("（无有效发言）")

    lines.append("\n## 按小时活跃分布（本地时区）\n")
    max_h = max(stats["hourly"]) or 1
    for h in range(24):
        c = stats["hourly"][h]
        lines.append(f"- `{h:02d}:00` | {_barh(c, max_h)} {c}")

    lines.append("\n## 按日活跃分布\n")
    lines.append(f"- 活跃天数: {stats['active_days']}")
    lines.append(f"- 日均消息: {stats['avg_per_day']}")
    if stats["peak_day"]:
        lines.append(f"- 峰值日: {stats['peak_day'][0]}（{stats['peak_day'][1]} 条）")
    if stats["daily"]:
        lines.append(f"- 最早: {stats['daily'][0][0]}    最晚: {stats['daily'][-1][0]}")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="群聊/私聊统计分析台（基于已解密库）")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--session", help="群名/联系人名关键词，或 username（如 xxx@chatroom）")
    ap.add_argument("--username", help="精确 username（与 --session 二选一，上游已消歧时用）")
    ap.add_argument("--top", type=int, default=15, help="发言排行 Top N（默认 15）")
    ap.add_argument("--out", help="输出 Markdown 文件路径（默认打印到 stdout）")
    ap.add_argument("--no-zstd", action="store_true",
                    help="跳过 zstd 解压（快；发信人仅用本库 Name2Id，群内业务号前缀不剥离）")
    add_time_args(ap)
    args = ap.parse_args()
    if not args.username and not args.session:
        sys.exit("[x] 需要 --session（群名/联系人）或 --username 精确定位")

    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)
    if since_ts or until_ts:
        print(f"[i] 时间过滤: {args.since or '不限'} ~ {args.until or '不限'} ({args.last or '全部'})")

    if args.username:
        contact_db = find_file(args.dec, "contact.db")
        rows = q(contact_db, "SELECT username, nick_name, remark FROM contact WHERE username=?",
                 (args.username,))
        if not rows:
            sys.exit(f"[x] username「{args.username}」在 contact.db 不存在")
        g = rows[0]
        uname = g["username"]
        disp = g["nick_name"] or g["remark"] or uname
        is_group = uname.endswith("@chatroom")
    else:
        uname, disp, is_group = resolve_session(args.dec, args.session)

    print(f"[i] 目标: {disp}  ({uname}, {'群' if is_group else '私聊'})")

    with_zstd = not args.no_zstd
    import time as _t
    t0 = _t.time()
    msgs, nick, db_hits = load_messages(args.dec, uname, since_ts, until_ts, with_zstd=with_zstd)
    elapsed = _t.time() - t0
    print(f"[i] 加载 {len(msgs)} 条（{db_hits} 个分库，{elapsed:.1f}s）"
          + ("，未解压 zstd" if not with_zstd else ""))

    if not msgs:
        sys.exit("[x] 时间范围内无消息")

    stats = compute_stats(msgs, top_n=args.top)
    report = render_report(disp, stats, db_hits, since_ts, until_ts)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n[✓] 报告已写入: {args.out}")
    else:
        print("\n" + report)

    print(f"\n--- 共 {stats['total']} 条（有效 {stats['effective']} / 系统 {stats['system']}）"
          f"，活跃 {stats['active_days']} 天 ---")


if __name__ == "__main__":
    main()
