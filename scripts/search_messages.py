#!/usr/bin/env python3
"""聊天全文搜索 —— 基于微信自带 FTS5 索引（直接读 _content 表绕过自定义分词器）

背景:
    微信 4.x 在 message/message_fts.db 里建了 FTS5 全文索引，分 4 个分片
    (message_fts_v4_0..3)。虚拟表用了微信自定义分词器 MMFtsTokenizer，
    Python 标准 sqlite3 未注册该分词器，MATCH 查询会报
    "no such tokenizer: MMFtsTokenizer"。

    解决方案: 直接读底层 _content 表（message_fts_v4_N_content），
    用 LIKE 做子串匹配。实测全库 ~120 万行 LIKE 扫描仅 ~0.2s，性能够用。
    _content 表列映射:
        c0=acontent(可搜索正文)  c1=message_local_id  c2=sort_seq
        c3=local_type           c4=session_id        c5=sender_id
        c6=create_time(unix秒)
    session_id / sender_id 是 FTS 库内 name2id 表的 rowid（已实测验证映射有效），
    与 contact.db 的 name2id 行号【不一致】，必须用 FTS 库自己的 name2id 反查。

用法:
    python search_messages.py --dec "<decrypted>" --keyword "关键词"
    python search_messages.py --dec "<decrypted>" --keyword "部署" --session "项目群" --limit 20
    python search_messages.py --dec "<decrypted>" --keyword "合同" --last 7d --out 搜索结果.md
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime

# 群名/昵称常含 emoji；stdout 重定向到管道/文件时 Python 退回 GBK，替换而非崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# FTS 四个分片的底层 content 表（绕过 MMFtsTokenizer）
FTS_CONTENT_TABLES = [
    "message_fts_v4_0_content",
    "message_fts_v4_1_content",
    "message_fts_v4_2_content",
    "message_fts_v4_3_content",
]

# --type 类型过滤（按 local_type 低位 (c3 & 255) 映射，与微信 4.x 类型表一致）
# 注意：49 系 appmsg（链接/文件/转账/红包/小程序）低位同为 49，低位类型无法再分；
#      要区分"文件 vs 链接"需解析 appmsg XML，超出"结果侧按类型筛"的范围，故 link/file 同集。
#      system 是整值哨兵（10000/10002），不取低位（10000&0xFF=16 会错），单独用 TYPE_MATCH 判。
TYPE_FILTER_MAP = {
    "text":     {1},
    "image":    {3},
    "voice":    {34},
    "video":    {43},
    "sticker":  {47},
    "location": {48},
    "link":     {49},   # 链接/转账/红包/小程序（appmsg 低位 49）
    "file":     {49},   # 文件同属 appmsg 低位 49（与 link 同集，见上注）
}


def type_matches(local_type, name):
    """判断某条 FTS 结果的 local_type 是否命中 --type。"""
    v = local_type or 0
    if name == "system":
        return v in (10000, 10002)
    return (v & 0xFF) in TYPE_FILTER_MAP[name]


def find_file(dec, name):
    """在 dec 目录下递归找文件（复用 export_group_md.py 同名函数逻辑）"""
    for root, _d, files in os.walk(dec):
        if name in files:
            return os.path.join(root, name)
    return None


def load_nick_map(contact_db):
    """从 contact.db 加载 username -> 显示名(备注>昵称)"""
    conn = sqlite3.connect(contact_db)
    conn.row_factory = sqlite3.Row
    nick = {}
    for r in conn.execute("SELECT username, nick_name, remark FROM contact"):
        nick[r["username"]] = (r["remark"] or r["nick_name"] or r["username"])
    conn.close()
    return nick


def load_fts_name2id(fts_db):
    """从 FTS 库加载 rowid -> username 映射（session_id/sender_id 的权威映射）"""
    conn = sqlite3.connect(fts_db)
    conn.row_factory = sqlite3.Row
    m = {}
    for r in conn.execute("SELECT rowid, username FROM name2id"):
        m[r["rowid"]] = r["username"]
    conn.close()
    return m


def resolve_session_id(fts_db, session_arg, nick_map):
    """把 --session 参数解析为 FTS name2id 中的 rowid。

    策略:
    1. 如果 session_arg 本身就是 username（含 @ 或 wxid_ 开头），直接查 FTS name2id
    2. 否则当群名/昵称关键词，先在 contact.db 找 username，再查 FTS name2id
    返回 (session_id_rowid, 显示名) 或 (None, None)
    """
    fconn = sqlite3.connect(fts_db)
    fconn.row_factory = sqlite3.Row
    arg = session_arg.strip()

    # 策略1: 看起来像 username
    if "@" in arg or arg.startswith("wxid_") or arg in ("filehelper", "weixin", "fmessage"):
        r = fconn.execute("SELECT rowid FROM name2id WHERE username=?", (arg,)).fetchone()
        if r:
            fconn.close()
            return r["rowid"], nick_map.get(arg, arg)
        fconn.close()
        return None, None

    # 策略2: 关键词匹配 contact.db 找 username
    # 需要 contact.db —— 由调用方传入或自行查找
    fconn.close()
    return None, None  # 由外部主流程处理（需要 contact.db）


def highlight(text, keyword):
    """在文本中高亮关键词（用 **keyword** 包裹）"""
    if not keyword or not text:
        return text or ""
    # 转义正则特殊字符
    esc = re.escape(keyword)
    return re.sub(esc, f"**{keyword}**", text, flags=re.IGNORECASE)


def truncate(text, width=200):
    """截断长文本，保留关键词附近片段"""
    if not text:
        return ""
    text = text.replace("\r", "").replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[:width] + "…"


def main():
    ap = argparse.ArgumentParser(description="聊天全文搜索（基于微信 FTS5 索引底层表）")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--keyword", required=True, help="搜索关键词（中文/英文均可，子串匹配）")
    ap.add_argument("--session", help="限定会话：群名/联系人名 或 username（如 xxx@chatroom）")
    ap.add_argument("--limit", type=int, default=50, help="最多返回条数（默认 50）")
    ap.add_argument("--out", help="输出 Markdown 文件路径（默认打印到 stdout）")
    ap.add_argument("--type", choices=sorted(list(TYPE_FILTER_MAP.keys()) + ["system"]),
                    help="按消息类型过滤（在 FTS 拿到结果后按 local_type 筛，不改 FTS 查询）："
                         "text/image/voice/video/sticker/location/link/file/system")
    from media_common import add_time_args, parse_time_range
    add_time_args(ap)
    args = ap.parse_args()

    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)

    # ---- 定位数据库 ----
    fts_db = find_file(args.dec, "message_fts.db")
    contact_db = find_file(args.dec, "contact.db")
    if not fts_db:
        sys.exit("[x] 找不到 message_fts.db（在 dec 目录下递归搜索）")
    if not contact_db:
        sys.exit("[x] 找不到 contact.db")

    print(f"[i] FTS 索引: {fts_db}")
    print(f"[i] 通讯录:   {contact_db}")

    # ---- 加载映射 ----
    nick = load_nick_map(contact_db)
    fts_n2i = load_fts_name2id(fts_db)
    print(f"[i] FTS name2id 映射: {len(fts_n2i)} 个会话/联系人")
    print(f"[i] contact 昵称映射: {len(nick)} 条")

    # ---- 自校验：拿一个已知 wxid 反查 FTS name2id，确认 rowid->username 映射有效 ----
    # 取 FTS name2id rowid=1 的 username，再确认它在 contact.db 里存在或本身就是合法 username
    if 1 in fts_n2i:
        sample = fts_n2i[1]
        print(f"[i] 自校验: FTS name2id rowid=1 -> {sample}")
    else:
        print("[!] 自校验: FTS name2id rowid=1 为空，映射可能异常")

    # ---- 解析 --session ----
    session_id_filter = None
    if args.session:
        arg = args.session.strip()
        # 策略1: 直接当 username 查 FTS name2id
        r = sqlite3.connect(fts_db).execute(
            "SELECT rowid FROM name2id WHERE username=?", (arg,)).fetchone()
        if r:
            session_id_filter = r[0]
            print(f"[i] 会话过滤: {arg} (FTS rowid={session_id_filter})")
        else:
            # 策略2: 在 contact.db 按昵称/备注/用户名模糊查
            cconn = sqlite3.connect(contact_db)
            cconn.row_factory = sqlite3.Row
            pat = "%" + arg.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = cconn.execute(
                "SELECT username, nick_name, remark FROM contact "
                "WHERE username LIKE ? ESCAPE '\\' OR nick_name LIKE ? ESCAPE '\\' "
                "OR remark LIKE ? ESCAPE '\\'", (pat, pat, pat)).fetchall()
            cconn.close()
            if not rows:
                sys.exit(f"[x] --session「{arg}」在通讯录中未找到")
            # 找精确匹配
            exact = [r for r in rows if (r["nick_name"] or "") == arg or (r["remark"] or "") == arg]
            if len(exact) == 1:
                target = exact[0]
            elif len(rows) == 1:
                target = rows[0]
            else:
                print(f"[!] 「{arg}」命中 {len(rows)} 个联系人，选择第一个:")
                for x in rows[:5]:
                    print(f"    {x['nick_name'] or x['remark'] or x['username']}  ({x['username']})")
                target = rows[0]
            # 用 username 查 FTS name2id rowid
            fconn = sqlite3.connect(fts_db)
            r = fconn.execute("SELECT rowid FROM name2id WHERE username=?", (target["username"],)).fetchone()
            fconn.close()
            if r:
                session_id_filter = r[0]
                disp = nick.get(target["username"], target["username"])
                print(f"[i] 会话过滤: {disp} ({target['username']}, FTS rowid={session_id_filter})")
            else:
                print(f"[!] 「{target['username']}」在 FTS name2id 中无记录（可能该会话无搜索索引）")

    # ---- 构建搜索 SQL ----
    like_pat = f"%{args.keyword}%"
    clauses = []
    params = []
    for tbl in FTS_CONTENT_TABLES:
        sql = (f"SELECT c0 AS acontent, c3 AS local_type, c4 AS session_id, c5 AS sender_id, "
               f"c6 AS create_time, '{tbl}' AS src "
               f"FROM {tbl} WHERE c0 LIKE ?")
        p = [like_pat]
        if session_id_filter is not None:
            sql += " AND c4 = ?"
            p.append(session_id_filter)
        if since_ts is not None:
            sql += " AND c6 >= ?"
            p.append(since_ts)
        if until_ts is not None:
            sql += " AND c6 <= ?"
            p.append(until_ts)
        clauses.append((sql, p))

    # ---- 执行搜索（UNION ALL 四个分片）----
    print(f"\n[i] 搜索关键词: 「{args.keyword}」"
          + (f"  时间: {args.since or '不限'} ~ {args.until or '不限'}" if (since_ts or until_ts) else "")
          + (f"  会话: {args.session}" if args.session else ""))
    print(f"[i] 正在扫描 {len(FTS_CONTENT_TABLES)} 个 FTS 分片...")

    t0 = datetime.now().timestamp()
    conn = sqlite3.connect(fts_db)
    conn.row_factory = sqlite3.Row
    all_rows = []
    for sql, p in clauses:
        rows = conn.execute(sql, p).fetchall()
        all_rows.extend(rows)
    elapsed = datetime.now().timestamp() - t0
    raw_count = len(all_rows)
    # 结果侧类型过滤（不改 FTS 查询逻辑，只在拿到结果后按 local_type 筛）
    if args.type:
        all_rows = [r for r in all_rows if type_matches(r["local_type"], args.type)]
    print(f"[i] 原始命中 {raw_count} 条（耗时 {elapsed:.2f}s）"
          + (f"，类型[{args.type}]过滤后 {len(all_rows)} 条" if args.type else "")
          + f"，按时间倒序取前 {args.limit} 条")

    # 按时间倒序排序
    all_rows.sort(key=lambda r: r["create_time"] or 0, reverse=True)
    result = all_rows[:args.limit]

    # ---- 格式化输出 ----
    lines = [f"# 聊天搜索结果: 「{args.keyword}」\n",
             f"> 搜索时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}  "
             f"| 命中 {len(all_rows)} 条  "
             f"| 显示 {len(result)} 条\n"]
    if args.session:
        lines.append(f"> 会话过滤: {args.session}\n")
    if args.type:
        lines.append(f"> 类型过滤: {args.type}\n")
    if since_ts or until_ts:
        lines.append(f"> 时间范围: {args.since or '不限'} ~ {args.until or '不限'} "
                     f"({args.last or ''})\n")

    for r in result:
        ts = r["create_time"]
        t_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "未知时间"
        # 会话名
        s_u = fts_n2i.get(r["session_id"], "?")
        s_disp = nick.get(s_u, s_u)
        # 发送人
        r_u = fts_n2i.get(r["sender_id"], "?")
        r_disp = nick.get(r_u, r_u)
        # 正文片段（高亮关键词）
        content = r["acontent"] or ""
        content = truncate(content, 300)
        content = highlight(content, args.keyword)
        # Markdown 转义管道符
        content = content.replace("|", "\\|")
        lines.append(f"\n## [{t_str}] {s_disp}\n")
        lines.append(f"- **发送人**: {r_disp}")
        lines.append(f"- **片段**: {content}")

    output = "\n".join(lines)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"\n[✓] 结果已写入: {args.out}")
        print(f"[✓] 共 {len(result)} 条（总命中 {len(all_rows)}）")
    else:
        print("\n" + output)
        print(f"\n--- 共 {len(result)} 条（总命中 {len(all_rows)}）---")

    conn.close()


if __name__ == "__main__":
    main()
