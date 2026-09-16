#!/usr/bin/env python3
"""微信 4.x 聊天文件索引（返回本地明文文件路径，不复制导出）

发现（本仓库独立研究）:
    微信把聊天中的文件（pdf/docx/zip/xlsx/apk/...）**明文**存在
    <账号目录>/msg/file/<YYYY-MM>/<原文件名>，不加密、不混淆。
    消息表 local_type=49 富文本里的 <appmsg><title>文件名</title>
    <appattach><totallen>字节数</totallen></appattach> 与文件一一对应。

场景:
    "帮我拿一下和 XXX / XXX 群里面的文件" -> 扫描该会话文件消息，
    匹配本地明文文件，**返回路径**（不复制、不移动原文件）。

用法:
    python export_files.py --account-dir "D:/微信数据/xwechat_files/<wxid>" \
        --dec "C:/Users/xxx/.wxcache/decrypted" --session "张三"
    python export_files.py --account-dir ... --dec ... --last 7d      # 近7天全部会话
    python export_files.py --account-dir ... --dec ... --session "XX群" --print-only  # 只看路径

输出:
    控制台逐条打印: <聊天时间> <发信人> <文件名> -> <本地路径>
    <out>/文件/files_map.json   （可选 --out；svr_id->文件+时间+路径，供 --files-map 嵌入）

依赖: 仅标准库 + 解密库目录（消息在解密后的 message_*.db）；文件本体是明文直读。
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from media_common import add_time_args, parse_time_range
except ImportError:
    sys.exit("[x] 缺少 media_common.py（同目录）")


def log(msg=""):
    print(msg, flush=True)


def find_dbs(dec, prefix):
    found = []
    for root, _d, files in os.walk(dec):
        for fn in files:
            if fn.startswith(prefix) and fn.endswith(".db"):
                found.append(os.path.join(root, fn))
    return sorted(found)


def load_nicknames(dec):
    nick = {}
    for cdb in find_dbs(dec, "contact"):
        try:
            conn = sqlite3.connect(cdb)
            for username, remark, nick_name in conn.execute(
                    "SELECT username, remark, nick_name FROM contact"):
                nick[username] = remark or nick_name or username
            conn.close()
        except Exception:
            pass
    return nick


def parse_appattach(xml):
    """从富文本 XML 提取文件信息 -> (title, totallen) 或 None"""
    if "<appattach>" not in xml:
        return None
    m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml, re.S)
    title = m.group(1).strip() if m else ""
    if not title or not re.search(r"\.\w{1,10}$", title):
        return None   # 无扩展名结尾视为非文件（小程序/链接/公众号卡片等）
    m = re.search(r"<totallen>(\d+)</totallen>", xml)
    totallen = int(m.group(1)) if m else None
    return title, totallen


def find_file_in_month(file_root, month, title, totallen):
    """在 msg/file/<month>/ 下按文件名+大小匹配原文件，返回绝对路径或 None"""
    d = os.path.join(file_root, month)
    if not os.path.isdir(d):
        return None
    candidates = []
    for fn in os.listdir(d):
        if fn == title:
            candidates.append((0, fn))
        else:
            # 变体：去掉 (N) 后缀后与 title 一致（如 a.pdf(1) / (1)a.pdf）
            stem = re.sub(r"^\((\d+)\)\s*|\s*\((\d+)\)(?=\.)", "", fn)
            if stem == title:
                candidates.append((1, fn))
    if not candidates:
        return None
    if totallen:
        for pri, fn in sorted(candidates):
            p = os.path.join(d, fn)
            if os.path.isfile(p) and os.path.getsize(p) == totallen:
                return p
    for pri, fn in sorted(candidates):
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p
    return None


def collect_file_msgs(dec, session_hash=None):
    """遍历 message_*.db 收集文件消息 (h, svr_id, create_time, username, title, totallen)"""
    out = []
    for db in find_dbs(dec, "message"):
        try:
            conn = sqlite3.connect(db)
            tabs = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            n2i = {}
            try:
                for r in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                    n2i[r[0]] = r[1]
            except Exception:
                pass
            for t in tabs:
                h = t[4:]
                if session_hash and h.lower() != session_hash.lower():
                    continue
                try:
                    rows = conn.execute(
                        f"SELECT server_id, create_time, real_sender_id, message_content "
                        f"FROM [{t}] WHERE (local_type & 255)=49").fetchall()
                except Exception:
                    continue
                for sid, ct, rid, content in rows:
                    if isinstance(content, bytes):
                        if content[:4] == b"\x28\xb5\x2f\xfd":
                            try:
                                import zstandard
                                content = zstandard.ZstdDecompressor().decompress(content)
                            except Exception:
                                continue
                        content = content.decode("utf-8", errors="replace")
                    if not isinstance(content, str):
                        continue
                    info = parse_appattach(content)
                    if not info:
                        continue
                    title, totallen = info
                    out.append((h, sid, ct, n2i.get(rid, ""), title, totallen))
            conn.close()
        except Exception as e:
            log(f"  [!] {os.path.basename(db)} 读取失败: {e}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-dir", required=True,
                    help="微信账号目录（xwechat_files/<wxid>，含 msg/file 明文文件）")
    ap.add_argument("--dec", required=True, help="解密库目录（消息在解密后的 message_*.db）")
    ap.add_argument("--session", help="只查指定会话（32 位表 hash 或联系人/群名）")
    ap.add_argument("--out", help="可选：把 files_map.json 写到该目录（不写则仅控制台打印）")
    ap.add_argument("--print-only", action="store_true",
                    help="只打印路径，不构建 files_map（更快）")
    add_time_args(ap)
    args = ap.parse_args()

    for d in (args.account_dir, args.dec):
        if not os.path.isdir(d):
            sys.exit(f"[x] 目录不存在: {d}")
    file_root = os.path.join(args.account_dir, "msg", "file")
    if not os.path.isdir(file_root):
        sys.exit(f"[x] 找不到明文文件目录（msg/file），确认账号目录正确: {file_root}")
    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)

    t0 = time.time()
    nick = load_nicknames(args.dec)

    session_hash = None
    if args.session:
        if len(args.session) == 32:
            session_hash = args.session.lower()
        else:
            rows = []
            for cdb in find_dbs(args.dec, "contact"):
                try:
                    conn = sqlite3.connect(cdb)
                    rows = conn.execute(
                        "SELECT username, remark, nick_name FROM contact "
                        "WHERE remark LIKE ? OR nick_name LIKE ?",
                        (f"%{args.session}%", f"%{args.session}%")).fetchall()
                    conn.close()
                    if rows:
                        break
                except Exception:
                    pass
            if len(rows) != 1:
                sys.exit(f"[x] 名称「{args.session}」匹配到 {len(rows)} 个会话，请用完整名称或 32 位 hash")
            session_hash = hashlib.md5(rows[0][0].encode()).hexdigest()
        log(f"  [√] 会话「{args.session}」-> hash {session_hash}")

    log(f"  [i] 扫描文件消息（local_type=49 + appattach）...")
    msgs = collect_file_msgs(args.dec, session_hash)
    log(f"  [i] 文件消息 {len(msgs)} 条")
    if since_ts or until_ts:
        before = len(msgs)
        msgs = [m for m in msgs
                if (since_ts is None or m[2] >= since_ts)
                and (until_ts is None or m[2] <= until_ts)]
        log(f"  [i] 时间过滤后 {before} -> {len(msgs)} 条")
    if not msgs:
        log("[i] 无文件消息（该会话可能没有文件，或未在本地同步）")
        return

    def hash2name(h):
        for cdb in find_dbs(args.dec, "contact"):
            try:
                conn = sqlite3.connect(cdb)
                for username, remark, nick_name in conn.execute(
                        "SELECT username, remark, nick_name FROM contact"):
                    if hashlib.md5(username.encode()).hexdigest() == h:
                        conn.close()
                        return remark or nick_name or username
                conn.close()
            except Exception:
                pass
        return h

    files_map = {}
    stats = {"msg": 0, "hit": 0, "miss": 0}
    by_session = {}
    for h, sid, ct, who_u, title, totallen in msgs:
        by_session.setdefault(h, []).append((sid, ct, who_u, title, totallen))

    for h, items in by_session.items():
        sname = hash2name(h)
        log(f"\n=== {sname} （{len(items)} 条文件消息）===")
        for sid, ct, who_u, title, totallen in sorted(items, key=lambda x: x[1]):
            stats["msg"] += 1
            month = datetime.fromtimestamp(ct).strftime("%Y-%m")
            src = find_file_in_month(file_root, month, title, totallen)
            who = nick.get(who_u, who_u or "?")
            ts = datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M")
            if not src:
                stats["miss"] += 1
                log(f"  - `{ts}` {who}  {title}  [本地未找到: msg/file/{month}/ 无此文件]")
                continue
            stats["hit"] += 1
            sz = os.path.getsize(src)
            log(f"  - `{ts}` {who}  {title}  ({sz/1024:.0f}KB)")
            log(f"      -> {src}")
            if not args.print_only:
                files_map.setdefault(h, {})[str(sid)] = {
                    "file": src, "ts": ct,
                    "time": datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M:%S"),
                    "who": who, "username": who_u or "", "session": sname,
                    "name": title, "size": sz}

    if files_map and args.out:
        out_root = os.path.join(args.out, "文件")
        os.makedirs(out_root, exist_ok=True)
        map_path = os.path.join(out_root, "files_map.json")
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump(files_map, f, ensure_ascii=False, indent=1)
        log(f"\n  [√] 文件索引已写: {map_path}")

    log(f"\n[√] 完成: 文件消息 {stats['msg']}，本地命中 {stats['hit']}，"
        f"未命中 {stats['miss']}，耗时 {time.time()-t0:.0f}s")
    if stats["miss"]:
        log("  [!] 未命中多为：原文件未在微信里点开下载过（仅 CDN 引用）/ 已过期清理")
    if stats["hit"] == 0:
        log("  [x] 0 命中 -> 原文件需先在微信客户端里点开过才会落盘 msg/file；或检查 --account-dir")


if __name__ == "__main__":
    main()
