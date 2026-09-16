#!/usr/bin/env python3
"""微信 4.x 聊天媒体索引（图片/视频/文件 → 本地路径，不复制导出）

场景: "帮我拿一下和 XXX / XXX 群里面的文件/视频" -> 返回本地缓存路径（不复制、不移动）。

本地存储规律与关联边界（本仓库实测，勿凭感觉改）:
    文件: local_type&255=49，XML <appattach><title>名</title>   -> msg/file/<月>/<原文件名>  明文
          ✅ 消息级可关联（XML title 匹配缓存文件名，重名带 (1) 前缀容错）
    图片: local_type&255=3，缓存 attach/<会话hash>/<月>/Img/<md5>.dat（V2 加密）
          ⚠️ 消息级**不可关联**：实测 XML 的 md5 与 dat 文件名 md5 交集=0（微信用另一套命名）
          ✅ 会话级可关联（attach 目录名即会话 hash）；dat 加密不能直接用，需 wx_export.py --media 解密
    视频: 缓存 msg/video/<月>/<md5>.mp4（明文）+ 同名 _thumb.jpg
          ⚠️ **无法按会话关联**：实测 XML md5 与 mp4 文件名 md5 交集=0，
          且 mp4 文件名 md5 不出现在消息 XML 任何字段（本地无关联键）——只能全量列出，按缩略图/月份人工识别
    共同前提: 只有用户在微信里【点开/下载过】的媒体才会落盘；未点开的显示"未命中/本地无缓存"。

用法:
    python export_media_index.py --account-dir "D:/微信数据/xwechat_files/<wxid>" \
        --dec "C:/xxx/.wxcache/decrypted" --session "张三" --type file
    python export_media_index.py --account-dir ... --dec ... --type all --last 7d
    python export_media_index.py --account-dir ... --dec ... --type video --out ./media_index

    --type: file|video|image|all（默认 all）；--out 可选写 media_index.json

依赖: 仅标准库 + 解密库（文件消息在 message_*.db）；图片 dat / 视频 mp4 均为直读路径。
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


def hash2name(dec, h):
    for cdb in find_dbs(dec, "contact"):
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


def collect_file_msgs(dec, session_hash, since_ts, until_ts):
    """文件消息（local_type 低8位=49 + appattach + title 带扩展名）。
    返回 [{h, sid, ct, who_u, title, totallen}]"""
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
                        f"FROM [{t}] WHERE (local_type & 255)=49 AND message_content IS NOT NULL").fetchall()
                except Exception:
                    continue
                for sid, ct, rid, content in rows:
                    if since_ts is not None and ct < since_ts:
                        continue
                    if until_ts is not None and ct > until_ts:
                        continue
                    if isinstance(content, bytes):
                        if content[:4] == b"\x28\xb5\x2f\xfd":
                            try:
                                import zstandard
                                content = zstandard.ZstdDecompressor().decompress(content)
                            except Exception:
                                continue
                        content = content.decode("utf-8", errors="replace")
                    if not isinstance(content, str) or "<appattach>" not in content:
                        continue
                    m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", content, re.S)
                    title = m.group(1).strip() if m else ""
                    if not title or not re.search(r"\.\w{1,10}$", title):
                        continue   # 无扩展名结尾视为非文件（小程序/链接/公众号卡片等）
                    m2 = re.search(r"<totallen>(\d+)</totallen>", content)
                    out.append(dict(h=h, sid=sid, ct=ct, who_u=n2i.get(rid, ""),
                                   title=title, totallen=int(m2.group(1)) if m2 else None))
            conn.close()
        except Exception as e:
            log(f"  [!] {os.path.basename(db)} 读取失败: {e}")
    return out


def find_file_in_month(file_root, month, title, totallen):
    d = os.path.join(file_root, month)
    if not os.path.isdir(d):
        return None
    candidates = []
    for fn in os.listdir(d):
        if fn == title:
            candidates.append((0, fn))
        else:
            stem = re.sub(r"^\(\d+\)\s*|\s*\(\d+\)(?=\.)", "", fn)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-dir", required=True,
                    help="微信账号目录（xwechat_files/<wxid>，含 msg/{attach,file,video}）")
    ap.add_argument("--dec", required=True, help="解密库目录（消息在解密后的 message_*.db）")
    ap.add_argument("--session", help="只查指定会话（32 位表 hash 或联系人/群名）")
    ap.add_argument("--type", default="all", choices=["file", "video", "image", "all"],
                    help="file=文件 / video=视频 / image=图片 / all=全部（默认 all）")
    ap.add_argument("--out", help="可选：把 media_index.json 写到该目录")
    add_time_args(ap)
    args = ap.parse_args()

    for d in (args.account_dir, args.dec):
        if not os.path.isdir(d):
            sys.exit(f"[x] 目录不存在: {d}")
    want = {"file": 4, "video": 2, "image": 1, "all": 7}[args.type]
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

    index = {}
    stats = {}

    # ---- 文件：消息级 title 关联（按会话 + 时间） ----
    if want & 4:
        log("  [i] 扫描文件消息（local_type&255=49 + appattach + title）...")
        fmsgs = collect_file_msgs(args.dec, session_hash, since_ts, until_ts)
        log(f"  [i] 文件消息 {len(fmsgs)} 条")
        fdir = os.path.join(args.account_dir, "msg", "file")
        hit = 0
        for m in sorted(fmsgs, key=lambda x: x["ct"]):
            month = datetime.fromtimestamp(m["ct"]).strftime("%Y-%m")
            p = find_file_in_month(fdir, month, m["title"], m["totallen"])
            who = nick.get(m["who_u"], m["who_u"] or "?")
            ts = datetime.fromtimestamp(m["ct"]).strftime("%Y-%m-%d %H:%M")
            sname = hash2name(args.dec, m["h"])
            if not p:
                log(f"  - `{ts}` {who}  [文件] {m['title']}  -> 未命中（未在微信点开/下载）")
                continue
            hit += 1
            sz = os.path.getsize(p)
            log(f"  - `{ts}` {who}  [文件] {m['title']}  ({sz/1024:.0f}KB)")
            log(f"      -> {p}")
            index.setdefault(m["h"], {}).setdefault("file", {})[str(m["sid"])] = {
                "path": p, "ts": m["ct"],
                "time": datetime.fromtimestamp(m["ct"]).strftime("%Y-%m-%d %H:%M:%S"),
                "who": who, "username": m["who_u"] or "", "session": sname,
                "name": m["title"], "size": sz}
        stats["file"] = f"{hit}/{len(fmsgs)}"

    # ---- 图片：会话级目录扫描（消息级不可关联） ----
    if want & 1:
        attach = os.path.join(args.account_dir, "msg", "attach")
        log("  [i] 图片：会话级扫描 attach/<会话hash>/<月>/Img/（XML md5 与 dat 名不对应，不做消息级）...")
        hit = 0
        hashes = [session_hash] if session_hash else (os.listdir(attach) if os.path.isdir(attach) else [])
        for h in sorted(hashes):
            ad = os.path.join(attach, h)
            if not os.path.isdir(ad):
                continue
            for month in sorted(os.listdir(ad)):
                if since_ts or until_ts:
                    try:
                        ym = datetime.strptime(month, "%Y-%m").timestamp()
                    except ValueError:
                        continue
                    if since_ts is not None and ym + 32 * 86400 < since_ts:
                        continue
                    if until_ts is not None and ym > until_ts + 32 * 86400:
                        continue
                idir = os.path.join(ad, month, "Img")
                if not os.path.isdir(idir):
                    continue
                # 图片缓存命名有变体：<md5>.dat / <md5>_h.dat / <md5>_t.dat / <md5>.dat_t.dat ...
                # 按 base 分组，组内取体积最大者（原图/高清最大），避免后缀猜测出错
                def img_base(f):
                    b = f
                    for suf in (".dat_t.dat", "_h.dat", "_t.dat", ".dat"):
                        if b.endswith(suf):
                            return b[:-len(suf)]
                    return b
                groups = {}
                for f in os.listdir(idir):
                    if not f.endswith(".dat"):
                        continue
                    groups.setdefault(img_base(f), []).append(f)
                if not groups:
                    continue
                sname = hash2name(args.dec, h)
                for base, variants in sorted(groups.items()):
                    hit += 1
                    pick = max(variants, key=lambda v: os.path.getsize(os.path.join(idir, v)))
                    p = os.path.join(idir, pick)
                    sz = os.path.getsize(p)
                    log(f"  - {sname} {month}  [图片] {base[:12]}...  ({sz/1024:.0f}KB)  [V2加密dat]")
                    log(f"      -> {p}")
                    index.setdefault(h, {}).setdefault("image", {})[base] = {
                        "path": p, "ts": None, "time": f"{month}",
                        "who": "", "username": "", "session": sname,
                        "name": base, "size": sz, "encrypted": True,
                        "note": "V2 加密 dat，需 wx_export.py --media 解密后才能作为图片使用"}
        if session_hash:
            log(f"  [i] 图片命中（该会话缓存 dat）: {hit}")
        else:
            log(f"  [i] 图片命中（全账号缓存 dat）: {hit}")
        stats["image"] = f"{hit}"

    # ---- 视频：全量清单（无法按会话关联） ----
    if want & 2:
        vdir = os.path.join(args.account_dir, "msg", "video")
        log("  [i] 视频：msg/video 全量扫描（本地数据无会话维度，无法按会话过滤）...")
        hit = 0
        for month in sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []:
            d = os.path.join(vdir, month)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".mp4"):
                    continue
                p = os.path.join(d, fn)
                mt = os.path.getmtime(p)
                if since_ts is not None and mt < since_ts:
                    continue
                if until_ts is not None and mt > until_ts:
                    continue
                hit += 1
                sz = os.path.getsize(p)
                thumb = os.path.join(d, fn[:-4] + "_thumb.jpg")
                log(f"  - {month}  [视频] {fn[:12]}...  ({sz/1024:.0f}KB)  缩略图={'有' if os.path.isfile(thumb) else '无'}")
                log(f"      -> {p}")
                index.setdefault("__video__", {}).setdefault("video", {})[fn] = {
                    "path": p, "ts": mt,
                    "time": datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M"),
                    "who": "", "username": "", "session": "（无法按会话关联，全量列出）",
                    "name": fn[:-4], "size": sz,
                    "thumb": thumb if os.path.isfile(thumb) else None}
        log(f"  [i] 视频命中: {hit}")
        stats["video"] = f"{hit}"

    if index and args.out:
        os.makedirs(args.out, exist_ok=True)
        mp = os.path.join(args.out, "media_index.json")
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=1)
        log(f"\n  [√] 索引已写: {mp}")

    log(f"\n[√] 完成  {stats}  耗时 {time.time()-t0:.0f}s")
    log("    * 未命中的媒体 = 未在微信客户端点开/下载过，不会落盘（微信机制）")
    log("    * 图片 dat 为 V2 加密，需 wx_export.py --media 解密导出为可用图片")


if __name__ == "__main__":
    main()
