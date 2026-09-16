#!/usr/bin/env python3
"""微信 4.x 媒体导出（图片 V2 解密 + WXGF 转码 + 视频复制）

用法:
    # 导出全部会话的图片（需要 media_keys.json，可用 extract_image_key.py 自动生成）
    python export_media.py --account-dir "D:\\微信数据\\xwechat_files\\wxid_xxx_xxxx" \
        --keys "<沙盒>\\media_keys.json" --out "D:\\媒体导出"

    # 只导出指定会话（--session 可给 32 位表 hash，或联系人/群名；配 --dec 才能按名解析）
    python export_media.py --account-dir "..." --keys "..." --out "..." --session "张三"

    # 同时复制视频（msg/video 下明文 mp4）
    python export_media.py --account-dir "..." --keys "..." --out "..." --video

    # 用解密库把会话 hash 映射成可读名称（输出目录用会话名）
    python export_media.py --account-dir "..." --keys "..." --dec "<沙盒>\\decrypted" --out "..."

输出:
    <out>/图片/<会话名或hash>/<月份>_<md5>[_t].jpg|png|webp|gif   （解密后图片）
    <out>/视频/<月份>/<原文件名>.mp4                             （视频为明文直拷）
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from media_common import (  # noqa: E402
    decrypt_dat_v2, detect_image_format, wxgf_to_image, find_voip_engine_dll,
    V2_MAGIC,
)


def log(msg=""):
    print(msg, flush=True)


def find_account_dir(p):
    p = os.path.abspath(p)
    if os.path.basename(p) == "db_storage":
        p = os.path.dirname(p)
    if os.path.basename(os.path.dirname(p)) == "xwechat_files" and os.path.isdir(p):
        return p
    xf = os.path.join(p, "xwechat_files")
    if os.path.isdir(xf):
        for d in os.listdir(xf):
            c = os.path.join(xf, d)
            if os.path.isdir(os.path.join(c, "msg", "attach")):
                return c
    return p if os.path.isdir(p) else None


def hash2name(dec_dir, session_hash):
    """把会话 32 位 hash 反查为可读名（备注优先，其次昵称）。dec_dir 为解密后的 contact.db 目录。"""
    if not dec_dir:
        return session_hash
    cdb = None
    for root, _d, files in os.walk(dec_dir):
        if "contact.db" in files:
            cdb = os.path.join(root, "contact.db")
            break
    if not cdb:
        return session_hash
    try:
        conn = sqlite3.connect(cdb)
        rows = conn.execute(
            "SELECT username, remark, nick_name FROM contact WHERE username != ''").fetchall()
        conn.close()
    except Exception:
        return session_hash
    for username, remark, nick_name in rows:
        if hashlib.md5(username.encode()).hexdigest() == session_hash:
            return remark or nick_name or username
    return session_hash


def load_keys(keys_path):
    with open(keys_path, encoding="utf-8") as f:
        k = json.load(f)
    aes_key = k.get("aes_key", "").encode("ascii")
    if len(aes_key) != 16:
        sys.exit(f"[x] media_keys.json 中 aes_key 无效: {k.get('aes_key')!r}")
    return aes_key, int(k.get("xor_key", 0)), k.get("wxid")


def iter_dat_files(attach_root, session_filter=None, since_ts=None, until_ts=None):
    """遍历 attach/<会话hash>/<月份>/Img/*.dat，返回 (session_hash, month, path)。
    时间过滤按月份目录近似（缓存目录月份 = 接收/下载月份）。"""
    for d in sorted(os.listdir(attach_root)):
        ad = os.path.join(attach_root, d)
        if not os.path.isdir(ad):
            continue
        if session_filter and d.lower() != session_filter.lower():
            continue
        for m in sorted(os.listdir(ad)):
            try:
                ym = datetime.strptime(m, "%Y-%m")
            except ValueError:
                continue
            if since_ts is not None and int(ym.replace(day=1).timestamp()) + 32 * 86400 < since_ts:
                continue
            if until_ts is not None and int(ym.replace(day=1).timestamp()) > until_ts + 32 * 86400:
                continue
            md = os.path.join(ad, m)
            img = os.path.join(md, "Img")
            if not os.path.isdir(img):
                continue
            for fn in sorted(os.listdir(img)):
                if fn.lower().endswith(".dat"):
                    yield d, m, os.path.join(img, fn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-dir", required=True,
                    help="账号目录（xwechat_files/<wxid>，或 db_storage / 数据根目录）")
    ap.add_argument("--keys", required=True, help="media_keys.json（extract_image_key.py 生成）")
    ap.add_argument("--out", required=True, help="媒体输出目录")
    ap.add_argument("--session", help="只导出指定会话（32 位表 hash，或联系人/群名，需 --dec）")
    ap.add_argument("--dec", help="解密库目录（可选，用于把会话 hash 映射成可读名称）")
    ap.add_argument("--video", action="store_true", help="同时复制视频（明文 mp4）")
    try:
        from media_common import add_time_args, parse_time_range
        add_time_args(ap)
    except ImportError:
        pass
    args = ap.parse_args()
    since_ts = until_ts = None
    try:
        since_ts, until_ts = parse_time_range(args.since, args.until, args.last)
    except Exception as e:
        sys.exit(f"[x] {e}")

    account = find_account_dir(args.account_dir)
    if not account:
        sys.exit(f"[x] 无法识别账号目录: {args.account_dir}")
    aes_key, xor_key, wxid = load_keys(args.keys)
    log(f"  [i] 账号: {os.path.basename(account)}  aes_key={aes_key.decode()} xor={xor_key}")

    attach = os.path.join(account, "msg", "attach")
    if not os.path.isdir(attach):
        sys.exit(f"[x] 账号目录下无 msg/attach: {attach}")

    # 会话筛选：--session 给可读名时先解析 hash（需 --dec）
    session_filter = args.session
    if args.session and len(args.session) != 32:
        if not args.dec:
            sys.exit(f"[x] --session 给了非 hash 名称「{args.session}」，需 --dec 解密库才能映射 hash")
        # 遍历 attach 找名称匹配的 hash（查 contact.db username -> md5）
        cdb = None
        for root, _d, files in os.walk(args.dec):
            if "contact.db" in files:
                cdb = os.path.join(root, "contact.db")
                break
        if not cdb:
            sys.exit("[x] --dec 下找不到 contact.db")
        conn = sqlite3.connect(cdb)
        rows = conn.execute(
            "SELECT username FROM contact WHERE remark LIKE ? OR nick_name LIKE ?",
            (f"%{args.session}%", f"%{args.session}%")).fetchall()
        conn.close()
        if len(rows) != 1:
            sys.exit(f"[x] 名称「{args.session}」匹配到 {len(rows)} 个会话，请改用完整名称或 32 位 hash")
        session_filter = hashlib.md5(rows[0][0].encode()).hexdigest()
        log(f"  [√] 会话「{args.session}」-> hash {session_filter}")

    # 会话名映射（图片输出目录用可读名；有解密库时按 username 反查备注/昵称）
    sessions = {}
    for d, _m, _p in iter_dat_files(attach, session_filter, since_ts, until_ts):
        sessions.setdefault(d, None)

    # ---- WXGF 转码器（按需懒加载）
    voip_dll = None
    wxgf_fail = 0

    out_img = os.path.join(args.out, "图片")
    stats = {"total": 0, "ok": 0, "fail": 0, "wxgf": 0, "wxgf_fail": 0, "skip_v1": 0}
    t0 = time.time()
    n_files = sum(1 for _ in iter_dat_files(attach, session_filter, since_ts, until_ts))
    log(f"  [i] 图片文件共 {n_files} 个，开始解密...")
    for i, (d, m, p) in enumerate(iter_dat_files(attach, session_filter, since_ts, until_ts), 1):
        stats["total"] += 1
        if i % 500 == 0:
            log(f"    ... {i}/{n_files}（成功 {stats['ok']} 失败 {stats['fail']}）")
        try:
            with open(p, "rb") as f:
                raw = f.read()
        except OSError:
            stats["fail"] += 1
            continue
        if not raw[:6] in (V2_MAGIC,):
            stats["skip_v1"] += 1
            continue  # V1 老格式/其他，跳过（不误判失败）
        out = decrypt_dat_v2(raw, aes_key, xor_key)
        fmt = detect_image_format(out)
        was_wxgf = False
        if fmt == "wxgf":
            if voip_dll is None:
                voip_dll = find_voip_engine_dll()
                if voip_dll:
                    log(f"  [i] WXGF 解码器: {voip_dll}")
            if not voip_dll:
                stats["wxgf_fail"] += 1
                stats["fail"] += 1
                continue
            jpg = wxgf_to_image(out, voip_dll)
            if not jpg:
                stats["wxgf_fail"] += 1
                stats["fail"] += 1
                continue
            out, fmt, was_wxgf = jpg, "jpg", True
            stats["wxgf"] += 1
        if not fmt:
            stats["fail"] += 1
            continue
        # 输出目录：图片/<会话名或hash>/<月份>_<文件名>
        sname = sessions.get(d)
        if not sname:
            sname = hash2name(args.dec, d) if args.dec else d
            sessions[d] = sname
        sdir = os.path.join(out_img, sname)
        os.makedirs(sdir, exist_ok=True)
        base = os.path.splitext(os.path.basename(p))[0]
        # WXGF 转码 = 缓存缩略图升级为完整原图，去掉 _t 后缀；直接解密保留原样
        if was_wxgf and base.endswith("_t"):
            base = base[:-2]
        with open(os.path.join(sdir, f"{m}_{base}.{fmt}"), "wb") as f:
            f.write(out)
        stats["ok"] += 1
    log(f"  [√] 图片解密完成: 成功 {stats['ok']}/{stats['total']}"
        f"（失败 {stats['fail']}，WXGF 转码 {stats['wxgf']}，跳过 V1/未知 {stats['skip_v1']}）")

    # ---- 视频（明文 mp4 直拷）
    if args.video:
        vdir = os.path.join(account, "msg", "video")
        if os.path.isdir(vdir):
            vout = os.path.join(args.out, "视频")
            vn = 0
            for root, _d, files in os.walk(vdir):
                for fn in files:
                    if fn.lower().endswith((".mp4", ".mov", ".avi", ".mkv")):
                        if since_ts is not None or until_ts is not None:
                            mt = os.path.getmtime(os.path.join(root, fn))
                            if since_ts is not None and mt < since_ts:
                                continue
                            if until_ts is not None and mt > until_ts:
                                continue
                        rel = os.path.relpath(root, vdir)
                        d = os.path.join(vout, rel)
                        os.makedirs(d, exist_ok=True)
                        try:
                            shutil.copy2(os.path.join(root, fn), os.path.join(d, fn))
                            vn += 1
                        except OSError:
                            pass
            log(f"  [√] 视频复制完成: {vn} 个（明文 mp4，无需解密）")
        else:
            log("  [!] 无 msg/video 目录，跳过视频")

    log(f"\n[√] 完成: {args.out}  耗时 {time.time() - t0:.0f}s")
    if stats["wxgf_fail"]:
        log(f"  [!] {stats['wxgf_fail']} 个 WXGF 文件转码失败（需微信安装目录的 VoipEngine.dll）")


if __name__ == "__main__":
    main()
