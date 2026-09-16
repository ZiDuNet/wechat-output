#!/usr/bin/env python3
"""digest_source.py — 群聊知识库素材包（v2.4 新增）

对指定群/联系人 + 时间范围，在 --outdir/<会话名>/sources/ 下生成一套素材包，
供下游知识库/群刊/AI 提炼消费：

    sources/messages.json   机器可读：每条消息 时间/发送人/类型/正文摘要（截断 200 字）
    sources/stats.json      与 chat_stats 同源的统计结构
    sources/material.md     人可读素材稿：话题概述、发言排行、关键消息摘录

大群/长时间范围单条正文已截断到 200 字，不把整库塞进一个文件。
统计与发信人解析复用 chat_stats（同目录 import），时间过滤走 media_common。

用法:
    python digest_source.py --dec "<decrypted>" --session "群名" --outdir "D:/素材包"
    python digest_source.py --dec "<decrypted>" --session "群名" --outdir "D:/素材包" --last 30d
    python digest_source.py --dec "<decrypted>" --username "xxx@chatroom" --outdir "D:/素材包" --since 2026-08-01
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

# 群名/昵称常含 emoji；stdout 重定向到管道/文件时 Python 退回 GBK，替换而非崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from chat_stats import (is_system_type, load_messages, resolve_session,
                        compute_stats, type_label)
from media_common import add_time_args, parse_time_range


def safe_name(name):
    """把会话名清洗成安全目录名（去 Windows 非法字符，压空白）。"""
    name = re.sub(r'[\\/:*?"<>|]+', "_", name or "").strip()
    name = re.sub(r"\s+", "_", name)
    return (name[:80] or "wechat_session")


def pick_key_messages(msgs, n=15):
    """挑"关键消息摘录"：取有效文本消息里正文最长的 n 条（长文通常是干货/讨论/通知）。"""
    cands = [m for m in msgs
             if not m["is_system"] and (m["local_type"] & 0xFF) == 1 and len(m["content"]) >= 20]
    cands.sort(key=lambda m: len(m["content"]), reverse=True)
    return cands[:n]


def build_material_md(name, stats, msgs, key_msgs):
    """人可读素材稿：话题概述 + 发言排行 + 关键消息摘录。"""
    span = stats["span"]
    lines = [f"# {name} — 群聊素材稿\n",
             f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
             f"素材条数: {stats['total']}\n"]
    if span:
        f0 = datetime.fromtimestamp(span[0]).strftime("%Y-%m-%d %H:%M")
        f1 = datetime.fromtimestamp(span[1]).strftime("%Y-%m-%d %H:%M")
        cover = int((span[1] - span[0]) / 86400) + 1
        lines.append(f"> 时间跨度: {f0}  →  {f1}（{cover} 天）\n")

    lines.append("\n## 话题概述\n")
    lines.append(f"- 有效消息 {stats['effective']} 条，系统/撤回/拍一拍 {stats['system']} 条")
    lines.append(f"- 活跃 {stats['active_days']} 天，日均 {stats['avg_per_day']} 条")
    if stats["peak_day"]:
        lines.append(f"- 峰值日 {stats['peak_day'][0]}（{stats['peak_day'][1]} 条）")
    lines.append("- 类型构成: " + "、".join(f"{l} {c}" for l, c in stats["type_dist"][:5]) + "\n")

    lines.append("\n## 发言排行\n")
    if stats["top_senders"]:
        eff = stats["effective"] or 1
        for i, (who, cnt) in enumerate(stats["top_senders"][:10], 1):
            lines.append(f"{i}. {who} — {cnt} 条（{cnt/eff*100:.1f}%）")
    lines.append("")

    lines.append("\n## 关键消息摘录（有效文本里正文最长的若干条，已截断）\n")
    if key_msgs:
        for m in key_msgs:
            t = datetime.fromtimestamp(m["ts"]).strftime("%Y-%m-%d %H:%M")
            lines.append(f"- `{t}` **{m['sender_disp']}**: {m['content']}")
    else:
        lines.append("（时间范围内无足够长的有效文本消息）")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="群聊知识库素材包生成器")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--session", help="群名/联系人名关键词，或 username")
    ap.add_argument("--username", help="精确 username（与 --session 二选一）")
    ap.add_argument("--outdir", required=True, help="素材包输出根目录（在其下建 <会话名>/sources/）")
    ap.add_argument("--top", type=int, default=15, help="发言排行 Top N（默认 15）")
    ap.add_argument("--excerpts", type=int, default=15, help="关键消息摘录条数（默认 15）")
    ap.add_argument("--no-zstd", action="store_true", help="跳过 zstd 解压（快；发信人精度略降）")
    add_time_args(ap)
    args = ap.parse_args()
    if not args.username and not args.session:
        sys.exit("[x] 需要 --session 或 --username")

    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)
    if since_ts or until_ts:
        print(f"[i] 时间过滤: {args.since or '不限'} ~ {args.until or '不限'} ({args.last or '全部'})")

    if args.username:
        from export_group_md import q, find_file
        contact_db = find_file(args.dec, "contact.db")
        rows = q(contact_db, "SELECT username, nick_name, remark FROM contact WHERE username=?",
                 (args.username,))
        if not rows:
            sys.exit(f"[x] username「{args.username}」在 contact.db 不存在")
        g = rows[0]
        uname = g["username"]
        disp = g["nick_name"] or g["remark"] or uname
    else:
        uname, disp, _is_group = resolve_session(args.dec, args.session)

    print(f"[i] 目标: {disp}  ({uname})")

    import time as _t
    t0 = _t.time()
    with_zstd = not args.no_zstd
    msgs, nick, db_hits = load_messages(args.dec, uname, since_ts, until_ts,
                                        with_zstd=with_zstd, content_cap=200)
    elapsed = _t.time() - t0
    print(f"[i] 加载 {len(msgs)} 条（{db_hits} 个分库，{elapsed:.1f}s）")
    if not msgs:
        sys.exit("[x] 时间范围内无消息")

    stats = compute_stats(msgs, top_n=args.top)
    key_msgs = pick_key_messages(msgs, n=args.excerpts)

    # 输出目录：<outdir>/<会话名>/sources/
    sess_dir = os.path.join(args.outdir, safe_name(disp))
    src_dir = os.path.join(sess_dir, "sources")
    os.makedirs(src_dir, exist_ok=True)

    # 1) messages.json（机器可读）
    messages_json = []
    for m in msgs:
        messages_json.append({
            "ts": m["ts"],
            "time": datetime.fromtimestamp(m["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            "sender": m["sender"],
            "sender_disp": m["sender_disp"],
            "type_low": (m["local_type"] or 0) & 0xFF,
            "type": type_label(m["local_type"]),
            "is_system": m["is_system"],
            "content": m["content"],
        })
    messages_path = os.path.join(src_dir, "messages.json")
    with open(messages_path, "w", encoding="utf-8") as f:
        json.dump(messages_json, f, ensure_ascii=False, indent=2)

    # 2) stats.json（与 chat_stats 同源；tuple/list 全部序列化成 JSON）
    stats_json = {
        "session": disp,
        "username": uname,
        "total": stats["total"],
        "effective": stats["effective"],
        "system": stats["system"],
        "type_dist": [{"type": l, "count": c} for l, c in stats["type_dist"]],
        "top_senders": [{"sender": s, "count": c} for s, c in stats["top_senders"]],
        "hourly": stats["hourly"],
        "daily": [{"date": d, "count": c} for d, c in stats["daily"]],
        "active_days": stats["active_days"],
        "avg_per_day": stats["avg_per_day"],
        "peak_day": {"date": stats["peak_day"][0], "count": stats["peak_day"][1]} if stats["peak_day"] else None,
        "span": {"first": stats["span"][0], "last": stats["span"][1]} if stats["span"] else None,
    }
    stats_path = os.path.join(src_dir, "stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats_json, f, ensure_ascii=False, indent=2)

    # 3) material.md（人可读素材稿）
    material_md = build_material_md(disp, stats, msgs, key_msgs)
    material_path = os.path.join(src_dir, "material.md")
    with open(material_path, "w", encoding="utf-8") as f:
        f.write(material_md)

    # 汇总日志
    def _size(p):
        return os.path.getsize(p)
    print(f"[✓] 素材包已生成: {src_dir}")
    print(f"    - messages.json : {_size(messages_path)/1024:.1f} KB  ({len(messages_json)} 条)")
    print(f"    - stats.json    : {_size(stats_path)/1024:.1f} KB")
    print(f"    - material.md   : {_size(material_path)/1024:.1f} KB")
    print(f"--- 共 {stats['total']} 条（有效 {stats['effective']} / 系统 {stats['system']}）---")


if __name__ == "__main__":
    main()
