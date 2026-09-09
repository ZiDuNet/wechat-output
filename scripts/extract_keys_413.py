#!/usr/bin/env python3
"""微信 4.1.13+ 数据库密钥提取（XOR 混淆破解版）

原理: 微信 4.1.13 把密钥对以 XOR 混淆的 x'<64hex key><32hex salt>' 字符串(99B 定长,
magic 'aa e0')存于 Config.Cipher 对象。本脚本扫进程内存 dump blobs → 去重 → 逐位置
求 XOR key(约束: 所有 blob 解密值落 hex 字符集) → salt 硬比对 + HMAC 校验 → all_keys.json。

零第三方依赖(校验/解密函数复用 wcdb_key_tool_windows.py)。
用法:
    python extract_keys_413.py --db-dir "G:\\...\\db_storage" --dump-dir "<沙盒>\\config_dump" --out "<沙盒>\\all_keys.json"
"""
import argparse
import collections
import ctypes
import ctypes.wintypes as wt
import hashlib
import itertools
import json
import os
import struct
import sys
import time
import glob

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

try:
    from wcdb_key_tool_windows import (collect_db_files, verify_enc_key,
        WINDOWS_CONFIG_CIPHER_NAME, _find_bytes_in_regions,
        _iter_windows_region_chunks, WINDOWS_MAX_USER_ADDRESS, WINDOWS_CONFIG_BLOB_MAX)
except ImportError:
    sys.exit("缺少 wcdb_key_tool_windows.py（GitHub: TANGandXUE/wcdb-key-tool），放到同目录再跑")

HEXCH = set(b"0123456789abcdefABCDEF")

kernel32 = ctypes.windll.kernel32
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
                ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
                ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD)]


def read_mem(h, addr, sz):
    buf = ctypes.create_string_buffer(sz)
    n = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, sz, ctypes.byref(n)):
        return buf.raw[: n.value]
    return None


def enum_regions(h):
    regs, addr, mbi = [], 0, MBI()
    while addr < 0x7FFFFFFFFFFF:
        if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < mbi.RegionSize < 500 * 1024 * 1024:
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def get_pids():
    import subprocess
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True, errors="replace", encoding="mbcs")
    pids = []
    for line in r.stdout.strip().split("\n"):
        p = line.strip('"').split('","')
        if len(p) >= 5:
            pids.append((int(p[1]), int(p[4].replace(",", "").replace(" K", "").strip() or "0")))
    return sorted(pids, key=lambda x: x[1], reverse=True)


def _u64(buf, off):
    return struct.unpack_from("<Q", buf, off)[0]


def dump_blobs(dump_dir):
    """扫微信进程内存, dump Config.Cipher blob(4.1.13: 99B 定长 XOR 混淆); 合并历史 blob 跨运行累积"""
    os.makedirs(dump_dir, exist_ok=True)
    seen, blobs = set(), []
    for fn in sorted(glob.glob(os.path.join(dump_dir, "blob_*.bin"))):
        data = open(fn, "rb").read()
        if data and data not in seen:
            seen.add(data)
            blobs.append(data)
    hist = len(blobs)
    idx = len(blobs)
    new = 0
    for pid, _kb in get_pids():
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            print(f"[WARN] cannot open pid {pid}", flush=True)
            continue
        try:
            regions = enum_regions(h)
            needle_addrs = _find_bytes_in_regions(regions, lambda b, s, _h=h: read_mem(_h, b, s),
                                                  WINDOWS_CONFIG_CIPHER_NAME)
            print(f"[*] pid={pid} needle occurrences: {len(needle_addrs)}", flush=True)
            if not needle_addrs:
                continue
            pair_patterns = [struct.pack("<Q", a) + struct.pack("<Q", len(WINDOWS_CONFIG_CIPHER_NAME))
                             for a in needle_addrs]
            for base, data in _iter_windows_region_chunks(regions, lambda b, s, _h=h: read_mem(_h, b, s), overlap=0x80):
                for pattern in pair_patterns:
                    pos = data.find(pattern)
                    while pos >= 0:
                        node = read_mem(h, base + pos - 0x10, 0x50)
                        if node and len(node) >= 0x40:
                            if _u64(node, 0x10) in needle_addrs and _u64(node, 0x18) == len(WINDOWS_CONFIG_CIPHER_NAME):
                                config_ptr = _u64(node, 0x28)
                                if 0x10000 <= config_ptr < WINDOWS_MAX_USER_ADDRESS:
                                    obj = read_mem(h, config_ptr + 0x88, 0x28)
                                    if obj and len(obj) >= 0x18:
                                        data_ptr, data_len = _u64(obj, 0x8), _u64(obj, 0x10)
                                        if 0 < data_len <= WINDOWS_CONFIG_BLOB_MAX and 0x10000 <= data_ptr < WINDOWS_MAX_USER_ADDRESS:
                                            blob = read_mem(h, data_ptr, int(data_len))
                                            if blob and len(blob) == data_len and blob not in seen:
                                                seen.add(blob)
                                                blobs.append(blob)
                                                with open(os.path.join(dump_dir, f"blob_{idx:03d}_pid{pid}_len{data_len}.bin"), "wb") as f:
                                                    f.write(blob)
                                                idx += 1
                                                new += 1
                        pos = data.find(pattern, pos + 1)
        finally:
            kernel32.CloseHandle(h)
    print(f"blobs: 历史 {hist} + 新增 {new} = 累计 unique {len(blobs)}", flush=True)
    return blobs


def filter_blobs(blobs):
    """剔除冒牌 blob: 先按众数长度分组, 再按众数 magic 前缀(前2字节)分组。

    实测: 真 blob 前2字节恒为 'x\\'' ^ k (同批一致, 如 aa e0); 内存中会混入同长度的
    其它对象(前2字节不同)。只用长度过滤分不开, 必须再按 magic 过滤。
    """
    def mode_group(items, keyfn):
        cnt = collections.Counter(keyfn(x) for x in items)
        top, n = cnt.most_common(1)[0]
        if len(cnt) > 1:
            detail = [(k if isinstance(k, int) else k.hex(), c) for k, c in cnt.most_common()]
            print(f"[i] 分组({len(cnt)}类): {detail} -> 采用 {top if isinstance(top,int) else top.hex()} x{n}", flush=True)
        return [x for x in items if keyfn(x) == top]

    groups = collections.Counter(len(b) for b in blobs)
    if len(groups) > 1:
        blobs = mode_group(blobs, len)
    return mode_group(blobs, lambda b: b[:2])


def solve_ks(blobs, hex_start, hex_end, pinned):
    """逐位置求 XOR key 候选(投票制, 抗噪声)。

    对每个位置枚举 256 个 k, 取"使最多 blob 解密值落 hex 字符集"的 k。
    不用全票制——混入噪声 blob 时某位置可能凑不出全票, 全票制会全盘崩。
    """
    L = len(blobs[0])
    ks = []
    for i in range(L):
        if i in pinned:
            ks.append([pinned[i] ^ blobs[0][i]])
            continue
        if not (hex_start <= i < hex_end):
            ks.append([0])
            continue
        col = [b[i] for b in blobs if len(b) > i]
        scored = sorted(((sum(1 for c in col if (c ^ k) in HEXCH), k)
                         for k in range(256)), reverse=True)
        best = scored[0][0]
        ks.append([k for v, k in scored if v == best] if best else [0])
    return ks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-dir", required=True, help="微信 db_storage 目录")
    ap.add_argument("--dump-dir", required=True, help="blob 落盘目录(沙盒)")
    ap.add_argument("--out", required=True, help="all_keys.json 输出路径")
    args = ap.parse_args()

    t0 = time.time()
    db_files, salt_to_dbs = collect_db_files(args.db_dir)
    print(f"DB files: {len(db_files)}, salts: {len(salt_to_dbs)}", flush=True)
    page1_by_salt = {s: p1 for _r, _p, _z, s, p1 in db_files}

    blobs = dump_blobs(args.dump_dir)
    if not blobs:
        print("FAIL: 未 dump 到 blob。确认微信已登录并打开过聊天窗口; "
              "若版本又变见 SKILL.md 踩坑#8", flush=True)
        sys.exit(2)
    blobs = filter_blobs(blobs)
    print(f"filtered blobs: {len(blobs)} 条, len={len(blobs[0])}", flush=True)

    found = {}  # rel -> (salt_hex, enc_key_hex)
    variants = [
        ("H1: x'...'(2+96+1)", (2, 98), {0: ord("x"), 1: ord("'"), 98: ord("'")}),
        ("H2: 3+96裸hex", (3, 99), {}),
        ("H3: 2+96+1纯hex", (2, 98), {}),
    ]
    for name, (hs, he), pinned in variants:
        ks = solve_ks(blobs, hs, he, pinned)
        ambig_idx = [i for i, k in enumerate(ks) if len(k) > 1]
        # 组合数保护: 每模糊位最多试 3 个候选, 总组合数封顶
        cands = [ks[i][:3] for i in ambig_idx]
        combos = list(itertools.product(*cands)) if cands else [()]
        if len(combos) > 20000:
            combos = combos[:20000]
        print(f"[{name}] 模糊位置 {ambig_idx}, 组合数 {len(combos)}", flush=True)
        for combo in combos:
            for i, k in zip(ambig_idx, combo):
                ks[i][0] = k
            for b in blobs:
                dec = bytes(c ^ (ks[i][0] if ks[i] else 0) for i, c in enumerate(b))
                hx = "".join(chr(c) for c in dec if chr(c) in "0123456789abcdefABCDEF")
                seg = hx[:96]
                if len(seg) < 96:
                    continue
                s_hex = seg[64:96].lower()
                if s_hex not in salt_to_dbs:
                    continue
                rel = salt_to_dbs[s_hex][0]
                if rel in found:
                    continue
                key = bytes.fromhex(seg[:64])
                p1 = page1_by_salt[s_hex]
                if verify_enc_key(key, p1):
                    found[rel] = (s_hex, key.hex())
                    print(f"  [DIRECT] {rel}", flush=True)
                else:
                    enc = hashlib.pbkdf2_hmac("sha512", key, bytes.fromhex(s_hex), 256000, dklen=32)
                    if verify_enc_key(enc, p1):
                        found[rel] = (s_hex, enc.hex())
                        print(f"  [PBKDF2] {rel}", flush=True)
        if len(found) >= len(db_files) * 0.9:
            break

    result = {rel: {"enc_key": k, "salt": s} for rel, (s, k) in found.items()}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n=== DONE {time.time()-t0:.0f}s: {len(found)}/{len(db_files)} ===", flush=True)
    for rel, _p, _z, s, p1 in db_files:
        if rel not in found:
            print(f"  MISSING: {rel}", flush=True)
    print(f"all_keys.json -> {args.out}", flush=True)
    sys.exit(0 if found else 3)


if __name__ == "__main__":
    main()
