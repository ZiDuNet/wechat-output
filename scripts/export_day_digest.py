#!/usr/bin/env python3
"""export_day_digest.py — 全天跨会话导出（v2.5 新增）

把某一天（或今天/昨天）【所有会话】的消息一次导出成一套 Markdown 梳理包：
    - _总览.md                  总量/分类统计 + 会话清单（含每会话文件链接）
    - NNN_<会话名>.md           大会话逐个一文件（按小时分节）
    - _其余会话合集.md          --merge-under N 时，小会话合并成一册（可选）
覆盖群聊 + 私聊 + 文件传输助手等全部 Msg_ 分表会话，天然适合"帮我梳理昨天的聊天"。

用法:
    python export_day_digest.py --dec "<decrypted>" --date 2026-09-16 --outdir "D:/导出/0916梳理"
    python export_day_digest.py --dec "<decrypted>" --date yesterday --outdir "D:/导出/昨天"
    python export_day_digest.py --dec ... --date 2026-09-16 --outdir ... --merge-under 100 --cap 200

设计要点（踩坑实录 #29 的落地）:
    - md5 映射不硬编码：从 contact.db 全量 username 建 {md5: username}（覆盖群+私聊），
      再扫各 message_N.db 的 Msg_% 表反查；映射外的孤儿分表计数上报、不静默丢弃。
    - local_type 哨兵按整值特判（同 chat_stats 口径）：10000=系统、10002=撤回、
      266287972401(62<<32|49)=拍一拍；⚠️ 244813135921(57<<32|49) 是【引用】复合类型，
      绝不能当系统消息，必须落 49 富文本分支解析 refermsg。
    - appmsg 子类型 <type> 不能锚定开头提取（XML 里 <title> 在 <type> 之前），
      必须 re.search 任意位置；转账/红包先查 wcpayinfo(wctype 2000/2001)，小程序查 weappinfo。
    - Msg_ 表没有 create_time 索引 → 时间过滤天然全表扫：过滤放 SQL WHERE（勿逐行 Python 判），
      11 个库全扫实测 ~15s。⚠️ 曾尝试用 session.db 的 last_timestamp 剪枝候选会话，实测**会丢数据**
      （SessionTable 懒落盘，last_timestamp 滞后于消息库：实测某群 last=09-15 23:18 而消息到 09-17），
      故已降级为 --prune 显式开启的调试选项，默认全扫保正确性。
    - 发信人两级定案同 export_group_md（踩坑#20）：内容前缀 "<id>:\\n" > 本库 Name2Id；
      无前缀 = 自己发的（微信不给自己加前缀）→ 显示「我」。
"""
import argparse
import hashlib
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

# 群名/昵称常含 emoji；stdout 重定向到管道/文件时 Python 退回 GBK，替换而非崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# 复用 export_group_md 的读取/前缀/解压/摘要工具（同目录脚本，import 不触发其 main）
from export_group_md import (ZSTD_MAGIC, find_file, q, rich_text_summary,
                             split_prefix, try_zstd)

# contact.db 之外的固定系统会话（部分账号不在 contact 表里，但确有 Msg_ 分表）
SPECIAL_ACCOUNTS = ("filehelper", "fmessage", "weixin", "mphelper", "floatbottle",
                    "medianote", "newsapp", "qmessage", "qqmail")

# 整值哨兵（绝不取低位——10000&0xFF=16、拍一拍低位是 49 会错桶）
T_SYSTEM, T_REVOKE, T_PAT = 10000, 10002, 266287972401
SYSTEM_TYPES = {T_SYSTEM, T_REVOKE, T_PAT}
# 低位类型 → [分类桶, 显示占位]
LOW_MEDIA = {3: "图片", 34: "语音", 43: "视频", 47: "表情", 48: "位置", 50: "通话"}


def _xml_text(s):
    """CDATA/标签剥离，取可读文本。"""
    if not s:
        return ""
    s = re.sub(r"<!\[CDATA\[|\]\]>", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def _tag(txt, name):
    m = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", txt, re.S)
    return m.group(1).strip() if m else ""


def rich_render(txt):
    """49 系富文本 -> (分类桶, 可读文本)。踩坑#29：`<type>` 不锚定开头；
    转账/红包/小程序优先于通用 <type> 判定。"""
    if "wcpayinfo" in txt:
        wt = re.search(r"<wctype>\s*(\d+)\s*</wctype>", txt)
        fee = _xml_text(_tag(txt, "feedesc"))
        memo = _xml_text(_tag(txt, "pay_memo"))
        kind = "转账" if (wt and wt.group(1) == "2000") else "红包"
        parts = [f"[{kind} {fee}]" if fee else f"[{kind}]"]
        if memo:
            parts.append(f"备注: {memo}")
        return "转账红包", " ".join(parts)
    if "weappinfo" in txt:
        return "小程序", f"[小程序] {_tag(txt, 'title')}".strip()
    n = re.search(r"<type>(\d+)</type>", txt)   # ⚠️ 不能 re.match：<title> 在 <type> 之前
    sub = n.group(1) if n else ""
    title = _tag(txt, "title")
    des = _tag(txt, "des")
    if sub == "6":
        return "链接文件", f"[文件] {title}".strip()
    if sub == "57":                              # 引用：带被引用人 + 被引用内容摘录
        ref = re.search(r"<refermsg>(.*?)</refermsg>", txt, re.S)
        who = _tag(ref.group(1), "displayname") if ref else ""
        quote = _xml_text(_tag(ref.group(1), "content"))[:60] if ref else ""
        head = f"[引用] {title}".strip() if title else "[引用]"
        if who:
            head += f"（回 @{who}" + (f": {quote}" if quote else "") + "）"
        return "引用", head
    if sub == "19":
        return "链接文件", f"[合并转发] {title}".strip()
    if sub in ("33", "36"):
        return "小程序", f"[小程序] {title}".strip()
    if title or des:
        return "链接文件", f"[链接] " + " | ".join(x for x in (title, des) if x)
    return "其他", "[富文本]"


def render(lt, txt, cap):
    """(local_type, 正文) -> (分类桶, 行文本)。文本内换行折成 ⏎ 保持单行。"""
    def fold(s):
        return re.sub(r"\s*\n\s*", " ⏎ ", s).strip()[:cap]

    if lt == T_SYSTEM:
        return "系统", "[系统] " + fold(_xml_text(txt) if txt.lstrip().startswith("<") else txt)
    if lt == T_REVOKE:
        return "系统", "[撤回]" + (f" {fold(txt)}" if txt and not txt.lstrip().startswith("<") else "")
    if lt == T_PAT:
        return "系统", "[拍一拍] " + fold(_xml_text(_tag(txt, "title")))
    low = (lt or 0) & 0xFF
    if low == 1:
        return "文本", fold(txt)
    if low in LOW_MEDIA:
        return "媒体", f"[{LOW_MEDIA[low]}]"
    if low == 49 and txt.lstrip().startswith("<"):
        bucket, text = rich_render(txt)
        return bucket, fold(text)
    if txt:                                       # 名片/未知类型的明文体
        return "其他", fold(f"[类型{lt}] " + txt)
    return "其他", f"[类型{lt}]"


def parse_date_arg(s):
    """'YYYY-MM-DD' / today|今天 / yesterday|昨天 -> (since_ts, until_ts) 左闭右开。"""
    s = (s or "").strip().lower()
    if s in ("today", "今天"):
        d = date.today()
    elif s in ("yesterday", "昨天"):
        d = date.today() - timedelta(days=1)
    else:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    since = datetime(d.year, d.month, d.day).timestamp()
    return since, since + 86400, d.strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser(description="全天跨会话导出（基于已解密库，v2.5）")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--date", required=True,
                    help="日期 YYYY-MM-DD，或 today/今天、yesterday/昨天")
    ap.add_argument("--outdir", required=True, help="输出目录（生成 _总览.md + NNN_<会话名>.md）")
    ap.add_argument("--cap", type=int, default=150, help="每条消息截断长度（默认 150 字）")
    ap.add_argument("--merge-under", type=int, default=0, metavar="N",
                    help="消息数少于 N 的会话不单独出文件，合并进 _其余会话合集.md（默认 0=关闭）")
    ap.add_argument("--prune", action="store_true",
                    help="⚠️ 调试用：按 session.db last_timestamp 剪枝候选分表提速（默认全扫）。"
                         "实测 SessionTable 懒落盘、last_timestamp 会滞后于消息库，"
                         "开启可能静默漏掉当天消息——出正式数据勿用")
    args = ap.parse_args()

    since_ts, until_ts, day_str = parse_date_arg(args.date)
    print(f"[i] 目标日期: {day_str}（时间窗 {datetime.fromtimestamp(since_ts):%Y-%m-%d %H:%M} ~ "
          f"{datetime.fromtimestamp(until_ts):%Y-%m-%d %H:%M}，左闭右开）")

    contact_db = find_file(args.dec, "contact.db")
    if not contact_db:
        sys.exit("[x] 找不到 contact.db")
    # md5 映射（踩坑#29）：contact 全量 username（群+私聊+公众号名片）+ 固定系统会话
    md5map = {hashlib.md5(u.encode()).hexdigest(): u
              for (u,) in q(contact_db, "SELECT username FROM contact WHERE username != ''")}
    for u in SPECIAL_ACCOUNTS:
        md5map[hashlib.md5(u.encode()).hexdigest()] = u
    nick = {r["username"]: (r["remark"] or r["nick_name"] or r["username"])
            for r in q(contact_db, "SELECT username, nick_name, remark FROM contact")}
    print(f"[i] md5 映射: {len(md5map)} 个会话（contact 全量 + 系统会话）")

    # 候选剪枝（--prune 才启用）：⚠️ 已证实不安全——SessionTable 懒落盘，last_timestamp
    # 滞后于消息库（实测某群 last=09-15 23:18，消息库实际到 09-17 00:46，剪掉整天 633 条）。
    # 保留此开关仅供加速调试，默认必须全扫。
    prune_map = None
    if args.prune:
        sdb = find_file(args.dec, "session.db")
        if sdb:
            try:
                # ⚠️ 表名是 SessionTable（不是 session）；last_timestamp = 该会话末条消息时间
                prune_map = {r["username"]: r["last_timestamp"]
                             for r in q(sdb, "SELECT username, last_timestamp FROM SessionTable")
                             if r["username"]}
                print(f"[i] session.db 就绪，{len(prune_map)} 个会话可做 last_timestamp 剪枝")
            except Exception as e:
                print(f"[!] session.db 读取失败（{e}），退化为全量扫描")

    t0 = time.time()
    known_users = set(nick) | set(md5map.values())
    seen = set()                       # 跨库去重（踩坑#19 口径）：(ts, rid, 内容md5)，只与前面的库比
    sessions = {}                      # username -> {"name":, "msgs": [(ts,bucket,who,text)], ...}
    n_tables = n_pruned = n_orphan = 0
    all_n2i_users = set()

    for root, _d, files in os.walk(args.dec):
        for fn in sorted(files):
            if not (fn.startswith("message") and fn.endswith(".db")):
                continue
            db = os.path.join(root, fn)
            try:
                n2i = {r["rid"]: r["user_name"]
                       for r in q(db, "SELECT rowid rid, user_name FROM Name2Id")}
            except Exception:
                n2i = {}
            all_n2i_users.update(n2i.values())
            for (t,) in q(db, "SELECT name FROM sqlite_master "
                              "WHERE type='table' AND name LIKE 'Msg_%'"):
                uname = md5map.get(t.split("_", 1)[1].lower())
                if not uname:
                    n_orphan += 1
                    continue
                n_tables += 1
                if prune_map is not None and uname in prune_map \
                        and prune_map[uname] < since_ts:
                    n_pruned += 1
                    continue
                rows = q(db, f"SELECT local_type, real_sender_id, create_time, sort_seq, "
                             f"message_content FROM [{t}] "
                             f"WHERE create_time >= ? AND create_time < ? "
                             f"ORDER BY create_time, sort_seq", (since_ts, until_ts))
                if not rows:
                    continue
                s = sessions.setdefault(uname, {"name": nick.get(uname, uname), "msgs": []})
                for r in rows:
                    c = r["message_content"]
                    ch = (hashlib.md5(c).hexdigest() if isinstance(c, bytes)
                          else hashlib.md5(str(c or "").encode("utf-8", "replace")).hexdigest())
                    key = (r["create_time"], r["real_sender_id"], ch)
                    if key in seen:
                        continue
                    seen.add(key)
                    # 解压 + 前缀提取（发信人两级定案，踩坑#20）
                    text = None
                    if isinstance(c, bytes) and c.startswith(ZSTD_MAGIC):
                        dec_t = try_zstd(c)
                        text = dec_t.replace("\r\n", "\n") if dec_t else ""
                    elif isinstance(c, bytes):
                        text = c.decode("utf-8", errors="replace")
                    else:
                        text = str(c or "")
                    sender_u = None
                    if isinstance(text, str):
                        sender_u, text = split_prefix(text, known_users)
                    bucket, line = render(r["local_type"], text or "", args.cap)
                    if sender_u:
                        who = nick.get(sender_u, sender_u)
                    elif r["local_type"] in SYSTEM_TYPES:
                        who = ""                     # 系统/撤回/拍一拍无发信人
                    else:
                        who = "我"                   # 无前缀 = 自己发的（踩坑#20 规则②）
                    s["msgs"].append((r["create_time"], bucket, who, line))

    elapsed = time.time() - t0
    known_users |= all_n2i_users
    sessions = {u: s for u, s in sessions.items() if s["msgs"]}
    if not sessions:
        sys.exit(f"[x] {day_str} 全天无任何会话消息（分表 {n_tables}，孤儿 {n_orphan}）。"
                 "若当天确有消息，请检查解密库是否过期（微信新消息未重新解密）。")

    # ---- 汇总
    order = sorted(sessions.items(), key=lambda kv: (-len(kv[1]["msgs"]), kv[1]["name"]))
    total = sum(len(s["msgs"]) for _u, s in order)
    bucket_total = {}
    for _u, s in order:
        for _ts, b, _w, _t in s["msgs"]:
            bucket_total[b] = bucket_total.get(b, 0) + 1
    buckets = ["文本", "媒体", "引用", "链接文件", "小程序", "转账红包", "系统", "其他"]
    print(f"[i] 扫描 {n_tables} 张分表（剪枝 {n_pruned}，孤儿 {n_orphan}），耗时 {elapsed:.1f}s")
    print(f"[i] 会话 {len(order)} 个 / 消息 {total} 条 / "
          + " · ".join(f"{b}:{bucket_total.get(b, 0)}" for b in buckets if bucket_total.get(b)))

    os.makedirs(args.outdir, exist_ok=True)
    # ---- 会话文件（大会话单独成文；--merge-under 以下并入合集）
    merged, standalone = [], []
    for i, (uname, s) in enumerate(order, 1):
        if args.merge_under and len(s["msgs"]) < args.merge_under:
            merged.append((uname, s))
        else:
            standalone.append((f"{len(standalone)+1:03d}", uname, s))
    links = []
    for num, uname, s in standalone:
        safe = re.sub(r'[\\/:*?"<>|]', "_", s["name"]).strip() or uname
        path = os.path.join(args.outdir, f"{num}_{safe}.md")
        write_session(path, day_str, s, buckets, bucket_total=False)
        links.append((s["name"], len(s["msgs"]), os.path.basename(path)))
    if merged:
        path = os.path.join(args.outdir, "_其余会话合集.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 其余会话合集 · {day_str}\n\n> {len(merged)} 个小会话"
                    f"（各少于 {args.merge_under} 条），合并成册。\n")
            for uname, s in merged:
                safe = re.sub(r'[\\/:*?"<>|]', "_", s["name"]).strip() or uname
                f.write(f"\n---\n\n# {s['name']}\n\n")
                f.write(session_body(s))
        links.append((f"其余 {len(merged)} 个小会话", sum(len(s['msgs']) for _u, s in merged),
                      "_其余会话合集.md"))
        print(f"[√] 合集: {path}（{len(merged)} 个会话）")

    # ---- 总览
    ov = [f"# {day_str} 全天会话总览\n",
          f"> 导出时间: {datetime.now():%Y-%m-%d %H:%M} · 来源: 本地解密库只读导出\n",
          "\n## 总量\n",
          f"- 会话: **{len(order)}** 个（有消息） · 消息: **{total}** 条",
          "- 分类: " + " · ".join(f"{b} {bucket_total.get(b, 0)}" for b in buckets
                                  if bucket_total.get(b)),
          f"- 分表扫描: {n_tables} 张全扫"
          + (f"（--prune 剪枝 {n_pruned} 张 ⚠️ 可能漏数据）" if args.prune else "")
          + f"，孤儿 {n_orphan}，{elapsed:.1f}s",
          "\n## 会话清单\n",
          "| # | 会话 | 条数 | 文件 |", "|---|---|---|---|"]
    for i, (name, cnt, fn) in enumerate(links, 1):
        ov.append(f"| {i} | {name} | {cnt} | [{fn}]({fn}) |")
    with open(os.path.join(args.outdir, "_总览.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(ov) + "\n")
    print(f"[√] 总览: {os.path.join(args.outdir, '_总览.md')}")
    print(f"\n消息口径: 共 {total} 条 = " + " + ".join(
        f"{b} {bucket_total[b]}" for b in buckets if bucket_total.get(b)))


def session_body(s):
    """会话正文：按小时分节。"""
    who_c = {}
    for _ts, b, w, _t in s["msgs"]:
        if w and w != "我":
            who_c[w] = who_c.get(w, 0) + 1
    top = sorted(who_c.items(), key=lambda kv: -kv[1])[:8]
    head = [f"> 消息 {len(s['msgs'])} 条 · 参与 {len(who_c) + (1 if any(w=='我' for _t,_b,w,_x in s['msgs']) else 0)} 人"
            + (f" · 发言最多：{'、'.join(f'{w}×{c}' for w, c in top)}" if top else ""),
            ""]
    cur_hour, out = None, list(head)
    for ts, b, w, t in s["msgs"]:
        hm = datetime.fromtimestamp(ts)
        if hm.hour != cur_hour:
            cur_hour = hm.hour
            out.append(f"\n## {hm.hour:02d} 时\n")
        if b == "系统":
            out.append(f"- `{hm:%H:%M}` {t}")
        else:
            out.append(f"- `{hm:%H:%M}` **{w}**: {t}")
    return "\n".join(out)


def write_session(path, day_str, s, _buckets, bucket_total=False):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {s['name']} · {day_str}\n\n")
        f.write(session_body(s) + "\n")


if __name__ == "__main__":
    main()
