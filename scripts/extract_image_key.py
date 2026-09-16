#!/usr/bin/env python3
"""微信 4.x 图片密钥提取（账号级，保存 media_keys.json）

原理（融合 WeChatDataAnalysis 的 derive_image_keys / scan_v2_templates / 内存扫描）：
- V2 图片解密需要 aes_key(16B) 与 xor_key(1B)
- 二者可由登录态整数 code 派生：aes_key = md5(f"{code}{wxid}")[:16]，xor = code & 0xFF
- code 常驻微信进程内存（4 字节 LE，含大量副本），可直接扫描获取 —— 全自动，无需用户操作
- 兜底：code 扫不到时轮询扫 32 字符 hex run（用户打开任意图片查看时 key 驻留内存）

输出 JSON（media_keys.json）:
    {"wxid": "...", "code": 123, "aes_key": "16hex", "xor_key": 73,
     "source": "memory_code" | "memory_key", "ts": 1234567890}

用法:
    python extract_image_key.py --account-dir "D:\\微信数据\\xwechat_files\\wxid_xxx_xxxx" \
        --out "<沙盒>\\media_keys.json"
    # --account-dir 也可以给 db_storage 上级目录（自动识别 xwechat_files/<wxid>）
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from media_common import (  # noqa: E402
    derive_image_keys, scan_v2_templates, verify_aes_key, aes_ecb_decrypt,
    scan_code_in_memory, scan_key_in_memory, detect_image_format, V2_MAGIC,
)


def find_account_dir(p):
    """入参可能是 db_storage / xwechat_files/<wxid> / 数据根目录，统一规整为存在的 xwechat_files/<wxid>。"""
    p = os.path.abspath(p)

    def pick(cands):
        # 优先有 msg/attach 的账号（真实媒体目录）；否则选 db_storage 库最多的
        with_media = [c for c in cands
                      if os.path.isdir(os.path.join(c, "msg", "attach"))]
        pool = with_media or cands
        if not pool:
            return None
        pool.sort(key=lambda c: -sum(
            len(files) for _r, _d, files in os.walk(os.path.join(c, "db_storage"))
            if os.path.isdir(os.path.join(c, "db_storage"))))
        return pool[0]

    if os.path.basename(p) == "db_storage":
        p = os.path.dirname(p)
    if os.path.basename(os.path.dirname(p)) == "xwechat_files":
        if os.path.isdir(p):
            return p
        # 传了不存在的 wxid 前缀（如漏了 _xxxx 设备后缀）-> 自动选真实账号
        parent = os.path.dirname(p)
        return pick([os.path.join(parent, d) for d in os.listdir(parent)
                     if os.path.isdir(os.path.join(parent, d))])
    xf = os.path.join(p, "xwechat_files")
    if os.path.isdir(xf):
        return pick([os.path.join(xf, d) for d in os.listdir(xf)
                     if os.path.isdir(os.path.join(xf, d))])
    # 兜底：p 本身存在但目录形态未知，直接用（模板扫描会给出明确报错）
    return p if os.path.isdir(p) else None


def candidate_wxids(account):
    """wxid 派生候选：完整目录名（权威）→ 去掉 _设备后缀的短名（部分版本派生用短名）。"""
    w = os.path.basename(account)
    out = [w]
    i = w.rfind("_")
    if i > 0 and w[i + 1:].isalnum() and w[:i].startswith("wxid_"):
        out.append(w[:i])
    return list(dict.fromkeys(out))


def log(msg=""):
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-dir", required=True,
                    help="账号目录（xwechat_files/<wxid>，或 db_storage / 数据根目录，自动规整）")
    ap.add_argument("--out", required=True, help="输出 media_keys.json 路径")
    ap.add_argument("--code", type=int, help="手动指定 code（跳过内存扫描；调试用）")
    ap.add_argument("--aes-key", help="手动指定 aes_key 16hex（跳过全部扫描；调试用）")
    ap.add_argument("--poll-timeout", type=int, default=240,
                    help="code 扫描失败时轮询扫 key 的超时秒数（默认 240）")
    args = ap.parse_args()

    account = find_account_dir(args.account_dir)
    if not account:
        sys.exit(f"[x] 无法识别账号目录: {args.account_dir}\n"
                 "     需指向 xwechat_files/<wxid>，或含 xwechat_files 的数据根目录")
    wxids = candidate_wxids(account)
    log(f"  [i] 账号目录: {account}")
    log(f"  [i] wxid 候选: {', '.join(wxids)}")

    # ---- 模板扫描：V2 文件头部首个密文块 + JPEG 尾部推断 XOR
    log("  [i] 扫描 V2 图片模板（推断 xor_key）...")
    tmpl = scan_v2_templates(account)
    if not tmpl["ciphertext"]:
        sys.exit("[x] 未找到任何 V2 图片文件（账号目录无 msg/attach/*/Img/*_t.dat？）\n"
                 "     请先在微信里收/看过一张图片，或检查 --account-dir 是否指向正确账号")
    log(f"  [√] 模板密文就绪，xor_key 推断 = {tmpl['xor_key']}（样本 {len(tmpl['templates'])} 个）")

    # ---- 手动指定（调试）
    if args.aes_key:
        key = args.aes_key.encode("ascii")
        ok = verify_aes_key(key, tmpl["ciphertext"])
        log(f"  [{'√' if ok else 'x'}] 手动 aes_key={args.aes_key} 验证{'通过' if ok else '失败'}")
        if not ok:
            sys.exit(1)
        result = {"wxid": wxids[0], "code": None, "aes_key": args.aes_key,
                  "xor_key": tmpl["xor_key"], "source": "manual", "ts": int(time.time())}
    elif args.code:
        # 用多形态 wxid 逐个验证派生
        for wxid in wxids:
            key, xor = derive_image_keys(args.code, wxid)
            if verify_aes_key(key, tmpl["ciphertext"]):
                log(f"  [√] 手动 code={args.code} wxid={wxid} -> aes={key.decode()} xor={xor} 验证通过")
                result = {"wxid": wxid, "code": args.code, "aes_key": key.decode(),
                          "xor_key": xor, "source": "manual", "ts": int(time.time())}
                break
        else:
            sys.exit(f"[x] 手动 code={args.code} 在所有 wxid 候选下派生密钥均验证失败")
    else:
        # ---- 主路径：内存扫 code（全自动）
        log("  [i] 扫描微信进程内存找 code（常驻登录态，全自动，无需打开图片）...")
        cands = scan_code_in_memory(wxids[0], xor_hint=tmpl["xor_key"], progress=log)
        result = None
        for code, _cnt in cands[:50000]:
            for wxid in wxids:
                key, xor = derive_image_keys(code, wxid)
                if verify_aes_key(key, tmpl["ciphertext"]):
                    log(f"  [√] 内存命中 code={code} (0x{code:x}) wxid={wxid} "
                        f"-> aes_key={key.decode()} xor={xor}")
                    result = {"wxid": wxid, "code": code, "aes_key": key.decode(),
                              "xor_key": xor, "source": "memory_code", "ts": int(time.time())}
                    break
            if result:
                break
        if result is None:
            log("  [!] code 扫描未命中，转轮询扫 key")

        # ---- 兜底：轮询扫 32hex key（需用户打开图片）
        if result is None:
            log(f"  [i] code 未命中。兜底：轮询扫内存 key（{args.poll_timeout}s）。\n"
                "     请在手机上给该微信账号发送一张图片并【点开查看】，或直接在这台电脑的微信里打开任意图片...")
            key = scan_key_in_memory(tmpl, timeout=args.poll_timeout, progress=log)
            if not key:
                sys.exit("[x] 轮询超时未命中。请确认：微信在运行且已登录？已按提示打开过图片？\n"
                         "     也可用 --code 手动指定（从 WeChatDataAnalysis 等工具曾捕获的值）")
            log(f"  [√] 内存命中 aes_key={key} xor={tmpl['xor_key']}")
            result = {"wxid": wxid, "code": None, "aes_key": key,
                      "xor_key": tmpl["xor_key"], "source": "memory_key", "ts": int(time.time())}

    # ---- 保存
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"  [√] 已保存: {args.out}")


if __name__ == "__main__":
    main()
