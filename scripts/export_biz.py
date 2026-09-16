#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务号 / 公众号文章导出（微信 4.x）

原理（本机实测确认，勿推翻）：
- 公众号推送单独存 message/biz_message_0.db，库里几百张 Msg_<md5(gh_username)> 分表；
- 表名 = "Msg_" + md5(公众号 user_name).hexdigest()（与私聊/群聊分表同规则）；
- Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER) 里 user_name 形如 gh_xxxxxxxx；
- message_content 是 zstd 压缩 BLOB（魔数 \\x28\\xb5\\x2f\\xfd），解压后是 <appmsg> XML，
  标题在 <title>、摘要在 <des>、原文链接在 <url>，<mmreader> 里还有封面 category/name；
- 公众号昵称去 contact/contact.db 的 contact 表（username → nick_name/remark）查。

用法:
    python export_biz.py --dec "<沙盒>\\decrypted" --out "G:\\导出\\公众号.md"
    python export_biz.py --dec "<沙盒>\\decrypted" --session gh_68b976f584b5 --out "新华网.md"
    python export_biz.py --dec "<沙盒>\\decrypted" --last 30d --out "近一月公众号.md"
"""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def zstd_decode(blob):
    """解 message_content BLOB：zstd 则解压，否则按 utf-8 直解。失败返回空串。"""
    if not blob:
        return ""
    if isinstance(blob, str):
        return blob
    if blob[:4] == ZSTD_MAGIC:
        try:
            import zstandard
            return zstandard.ZstdDecompressor().decompress(blob).decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        return blob.decode("utf-8", "replace")
    except Exception:
        return ""


def xml_text(xml, tag):
    """提取 <tag><![CDATA[...]]></tag> 或 <tag>...</tag> 的文本，去空白。"""
    m = re.search(r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (tag, tag), xml, re.S)
    return m.group(1).strip() if m else ""


def load_nicknames(dec):
    """读 contact.db：username → (nick_name, remark)。读不到不致命。"""
    nick, remark = {}, {}
    db = os.path.join(dec, "contact", "contact.db")
    if not os.path.isfile(db):
        return nick, remark
    try:
        conn = sqlite3.connect(db)
        for u, n, r in conn.execute("SELECT username, nick_name, remark FROM contact"):
            if n:
                nick[u] = n
            if r:
                remark[u] = r
        conn.close()
    except Exception as e:
        print(f"  [!] 读 contact.db 失败（不影响主流程）: {e}")
    return nick, remark


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def main():
    ap = argparse.ArgumentParser(description="服务号/公众号文章导出")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--out", required=True, help="输出 Markdown 路径")
    ap.add_argument("--session", help="只导出指定公众号 username（如 gh_xxxxxxxx）")
    from media_common import add_time_args, parse_time_range
    add_time_args(ap)
    args = ap.parse_args()

    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)
    biz_db = os.path.join(args.dec, "message", "biz_message_0.db")
    if not os.path.isfile(biz_db):
        sys.exit(f"[x] 找不到 {biz_db}（--dec 指错了？）")

    nick_map, remark_map = load_nicknames(args.dec)
    conn = sqlite3.connect(biz_db)
    conn.row_factory = sqlite3.Row

    # 1) Name2Id → gh_ 会话列表
    gh_users = []
    for r in conn.execute("SELECT user_name, is_session FROM Name2Id"):
        u = r["user_name"]
        if u and u.startswith("gh_") and (r["is_session"] or args.session):
            gh_users.append(u)
    if args.session:
        gh_users = [u for u in gh_users if u == args.session]
        if not gh_users:
            sys.exit(f"[x] Name2Id 里没有 {args.session}（用 --session 传完整 gh_xxx）")

    # 2) 收集每张分表
    existing = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")}
    rows_out = []          # (公众号显示名, 公众号username, 推送时间, 标题, 摘要, url)
    skipped_tables = 0
    for u in sorted(gh_users):
        tbl = "Msg_" + hashlib.md5(u.encode()).hexdigest()
        if tbl not in existing:
            skipped_tables += 1
            continue
        sql = f"SELECT create_time, message_content FROM {tbl}"
        cond, params = [], []
        if since_ts:
            cond.append("create_time >= ?"); params.append(since_ts)
        if until_ts:
            cond.append("create_time <= ?"); params.append(until_ts)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY create_time ASC"
        disp = remark_map.get(u) or nick_map.get(u) or u
        for row in conn.execute(sql, params):
            xml = zstd_decode(row["message_content"])
            title = xml_text(xml, "title") or "(无标题)"
            des = xml_text(xml, "des")
            url = xml_text(xml, "url")
            rows_out.append((disp, u, row["create_time"], title, des, url))
    conn.close()

    # 3) 写 Markdown
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    lines = ["# 公众号 / 服务号文章导出", ""]
    lines.append(f"- 导出时间：{fmt_ts(datetime.now().timestamp())}")
    rng = []
    if since_ts: rng.append(f"起 {fmt_ts(since_ts)}")
    if until_ts: rng.append(f"止 {fmt_ts(until_ts)}")
    lines.append(f"- 时间范围：{' '.join(rng) if rng else '全部'}")
    lines.append(f"- 公众号数：{len(gh_users)}（其中 {skipped_tables} 个无分表，跳过）")
    lines.append(f"- 文章总数：{len(rows_out)}")
    lines.append("")

    # 按公众号分组
    from collections import OrderedDict
    grouped = OrderedDict()
    for disp, u, ts, title, des, url in rows_out:
        grouped.setdefault((disp, u), []).append((ts, title, des, url))

    for (disp, u), items in grouped.items():
        lines.append(f"## {disp}  `{u}`  （{len(items)} 篇）")
        lines.append("")
        for ts, title, des, url in items:
            lines.append(f"### {fmt_ts(ts)}  {title}")
            if des:
                lines.append(f"> {des}")
            if url:
                lines.append(f"原文：{url}")
            lines.append("")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[ok] 公众号 {len(grouped)} 个，文章 {len(rows_out)} 篇 -> {args.out}")
    for (disp, u), items in list(grouped.items())[:10]:
        print(f"   - {disp} ({u}): {len(items)} 篇")
    if len(grouped) > 10:
        print(f"   ... 另有 {len(grouped)-10} 个公众号")


if __name__ == "__main__":
    main()
