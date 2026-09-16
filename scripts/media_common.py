#!/usr/bin/env python3
"""微信 4.x 媒体解密共享库（图片 V2 解密 / 密钥提取 / WXGF 转码 / 视频定位）

融合自开源参考实现 WeChatDataAnalysis（image_key_resolver / image_key_memory_scan /
media_helpers）的算法，重写为零第三方依赖的 Windows 版本：

- AES-128-ECB：走系统 bcrypt.dll（CNG），无需 pycryptodome/cryptography
- V2 图片格式：签名 07 08 56 32 08 07 + aes_size(LE u32) + xor_size(LE u32) + pad(1B)
  → AES 区(ECB+PKCS7) + 明文区 + XOR 区(逐字节 ^ xor_key)
- 图片密钥派生（账号级固定）：aes_key = md5(f"{code}{wxid}")[:16]（16 字符 ASCII），
  xor_key = code & 0xFF。code 是微信登录态整数（uin 类），常驻进程内存，可直接扫描获取。
- WXGF（微信自研图片容器，本地缓存未查看原图）：用微信自带 VoipEngine.dll 的
  wxam_dec_wxam2pic_5 转码为 jpg。

所有函数无机器路径硬编码；账号目录 / 输出目录由调用方显式传入。
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import os
import struct
import subprocess
import sys
import time

V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
V1_MAGIC = b"\x07\x08\x56\x31\x08\x07"
AES_BLOCK = 16

# ---------------------------------------------------------------- AES (bcrypt.dll)

_bcrypt = None
if sys.platform == "win32":
    _bcrypt = ctypes.WinDLL("bcrypt")
    _bcrypt.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(wt.HANDLE), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    _bcrypt.BCryptSetProperty.argtypes = [wt.HANDLE, ctypes.c_wchar_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong]
    _bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        wt.HANDLE, ctypes.POINTER(wt.HANDLE), ctypes.c_char_p, ctypes.c_ulong,
        ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong,
    ]
    _bcrypt.BCryptDecrypt.argtypes = [
        wt.HANDLE, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong,
    ]
    _bcrypt.BCryptDestroyKey.argtypes = [wt.HANDLE]
    _bcrypt.BCryptCloseAlgorithmProvider.argtypes = [wt.HANDLE, ctypes.c_ulong]


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-ECB 解密（无 padding），CNG bcrypt.dll，零第三方依赖。"""
    if _bcrypt is None:
        raise RuntimeError("非 Windows 平台暂不支持")
    h_alg = wt.HANDLE()
    status = _bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0)
    if status != 0:
        raise RuntimeError(f"BCryptOpenAlgorithmProvider failed: {status:#x}")
    try:
        mode = ("ChainingModeECB\x00").encode("utf-16-le")
        status = _bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, len(mode), 0)
        if status != 0:
            raise RuntimeError(f"BCryptSetProperty failed: {status:#x}")
        h_key = wt.HANDLE()
        status = _bcrypt.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0, key, len(key), 0)
        if status != 0:
            raise RuntimeError(f"BCryptGenerateSymmetricKey failed: {status:#x}")
        try:
            out_buf = ctypes.create_string_buffer(len(data))
            result_len = ctypes.c_ulong(0)
            status = _bcrypt.BCryptDecrypt(
                h_key, data, len(data), None,
                None, 0,
                out_buf, len(out_buf), ctypes.byref(result_len), 0,
            )
            if status != 0:
                raise RuntimeError(f"BCryptDecrypt failed: {status:#x}")
            return out_buf.raw[: result_len.value]
        finally:
            _bcrypt.BCryptDestroyKey(h_key)
    finally:
        _bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= AES_BLOCK and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data  # 非 PKCS7（如数据本身不以填充结尾）原样返回


# ---------------------------------------------------------------- V2 解密

def decrypt_dat_v2(data: bytes, aes_key: bytes, xor_key: int) -> bytes:
    """微信 4.x V2 图片解密：AES-ECB(区) + 明文区 + XOR(区)。"""
    if len(data) < 0xF + AES_BLOCK:
        return b""
    header, rest = data[:0xF], data[0xF:]
    sig, aes_size, xor_size = struct.unpack("<6sLLx", header)
    if sig not in (V2_MAGIC, V1_MAGIC):
        return b""
    aes_size += AES_BLOCK - aes_size % AES_BLOCK
    aes_data = rest[:aes_size]
    try:
        decrypted = pkcs7_unpad(aes_ecb_decrypt(aes_key[:AES_BLOCK], aes_data))
    except Exception:
        return b""
    if xor_size > 0:
        raw = rest[aes_size:-xor_size]
        xored = bytes(b ^ xor_key for b in rest[-xor_size:])
    else:
        raw, xored = rest[aes_size:], b""
    return decrypted + raw + xored


def detect_image_format(data: bytes) -> str | None:
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:5] in (b"GIF87", b"GIF89"):
        return "gif"
    if data[:4] in (b"wxgf", b"WXGF"):
        return "wxgf"
    return None


def derive_image_keys(code: int, wxid: str) -> tuple[bytes, int]:
    """WeFlow 风格密钥派生：aes_key = md5(code+wxid) hex 前 16 字符，xor = code & 0xFF。"""
    digest = hashlib.md5(f"{code}{wxid}".encode("utf-8")).hexdigest()
    return digest[:16].encode("ascii"), code & 0xFF


# ---------------------------------------------------------------- 模板扫描（XOR 推断）

def infer_xor_from_tail(tail: bytes) -> int | None:
    """JPEG 尾部 FF D9 被 XOR：tail 字节 ^ FF / ^ D9 应一致。"""
    if len(tail) != 2:
        return None
    a, b = tail[0] ^ 0xFF, tail[1] ^ 0xD9
    return a if a == b else None


def find_v2_template_files(account_dir: str, limit: int = 64):
    """找最近的 V2 图片文件（优先 attach/<hash>/<月份>/Img/ 下最新 _t.dat）。"""
    found = []  # (mtime_ns, path)
    attach_root = os.path.join(account_dir, "msg", "attach")
    for d in os.listdir(attach_root) if os.path.isdir(attach_root) else []:
        ad = os.path.join(attach_root, d)
        if not os.path.isdir(ad):
            continue
        for m in os.listdir(ad):
            md = os.path.join(ad, m)
            if not os.path.isdir(md):
                continue
            img = os.path.join(md, "Img")
            if not os.path.isdir(img):
                continue
            for fn in os.listdir(img):
                if fn.lower().endswith("_t.dat"):
                    p = os.path.join(img, fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    found.append((st.st_mtime_ns, p))
    found.sort(reverse=True)
    return [p for _, p in found[:limit]]


def scan_v2_templates(account_dir: str, limit: int = 32) -> dict:
    """从 V2 文件收集模板（首个 AES 密文块）+ 推断 XOR key。返回 {ciphertext, xor_key, templates}。"""
    templates = []  # (path, ciphertext16, tail_xor)
    for p in find_v2_template_files(account_dir, limit * 4):
        try:
            with open(p, "rb") as f:
                head = f.read(0xF + AES_BLOCK)
                if len(head) < 0xF + AES_BLOCK or head[:6] != V2_MAGIC:
                    continue
                f.seek(-2, os.SEEK_END)
                tail = f.read(2)
        except OSError:
            continue
        xor = infer_xor_from_tail(tail)
        templates.append((p, head[0xF:0xF + AES_BLOCK], xor))
        if len(templates) >= limit:
            break
    if not templates:
        return {"ciphertext": None, "xor_key": None, "templates": []}
    # XOR 众数
    from collections import Counter
    xors = Counter(t[2] for t in templates if t[2] is not None)
    xor_key = xors.most_common(1)[0][0] if xors else None
    return {
        "ciphertext": templates[0][1],
        "xor_key": xor_key,
        "templates": templates,
    }


def verify_aes_key(aes_key: bytes, ciphertext: bytes) -> bool:
    try:
        pt = aes_ecb_decrypt(aes_key[:AES_BLOCK], ciphertext)
    except Exception:
        return False
    return detect_image_format(pt) is not None


# ---------------------------------------------------------------- 进程内存

MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
MAX_ADDR = 0x7FFFFFFFFFFF


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
                ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
                ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD)]


def read_mem(h, addr, sz):
    buf = ctypes.create_string_buffer(sz)
    n = ctypes.c_size_t(0)
    if ctypes.windll.kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, sz, ctypes.byref(n)):
        return buf.raw[: n.value]
    return None


def enum_regions(h):
    regs, addr, mbi = [], 0, MBI()
    while addr < MAX_ADDR:
        if ctypes.windll.kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < mbi.RegionSize < 500 * 1024 * 1024:
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def find_wechat_pids():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, errors="replace", encoding="mbcs")
    except Exception:
        return []
    pids = []
    for line in r.stdout.strip().split("\n"):
        p = line.strip('"').split('","')
        if len(p) >= 5:
            try:
                pids.append((int(p[1]), int(p[4].replace(",", "").replace(" K", "").strip() or "0")))
            except ValueError:
                continue
    return sorted(pids, key=lambda x: x[1], reverse=True)


def scan_code_in_memory(wxid: str, xor_hint: int | None = None, progress=None):
    """扫微信进程内存找 code 候选（4 字节 LE 整数，常驻登录态）。

    预过滤：xor_hint 已知时只扫低字节 == xor_hint 的窗口（1/256，快 ~200 倍）；
    返回 [(code, 出现次数), ...] 按次数降序。真实 code 在内存有大量副本（实测数百处），
    由调用方按序派生密钥验证模板密文即可命中。
    """
    kernel32 = ctypes.windll.kernel32
    PROCESS_VM_READ, PROCESS_QUERY = 0x0010, 0x0400
    candidates = {}
    for pid, _kb in find_wechat_pids():
        h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY, False, pid)
        if not h:
            continue
        try:
            for base, size in enum_regions(h):
                off = 0
                while off < size:
                    sz = min(8 * 1024 * 1024, size - off)
                    data = read_mem(h, base + off, sz)
                    off += sz
                    if not data:
                        continue
                    n = len(data) - 3
                    i = 0
                    while i < n:
                        b = data[i]
                        if xor_hint is None or b == xor_hint:
                            code = struct.unpack_from("<I", data, i)[0]
                            if code:
                                candidates[code] = candidates.get(code, 0) + 1
                        i += 1
        finally:
            kernel32.CloseHandle(h)
        if progress:
            progress(f"  [i] pid={pid} 扫描完成，候选 code 累计 {len(candidates)} 个")
    if not candidates:
        return []
    best = sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)
    if progress:
        progress(f"  [i] 候选 code 共 {len(best)} 个，按出现次数降序逐个验证"
                 f"（前几名: {', '.join(str(c) for c, _ in best[:5])}）")
    return best


def scan_key_in_memory(template_scan, timeout=180, interval=5, progress=None):
    """轮询扫微信进程内存找 32 字符 hex run（图片渲染时 key 驻留）。
    用于 code 扫描失败时的兜底。返回 aes_key 字符串或 None。"""
    ciphertext = template_scan.get("ciphertext")
    if not ciphertext:
        return None
    kernel32 = ctypes.windll.kernel32
    PROCESS_VM_READ, PROCESS_QUERY = 0x0010, 0x0400
    HEXCH = set(b"0123456789abcdefABCDEF")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for pid, _kb in find_wechat_pids():
            h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY, False, pid)
            if not h:
                continue
            try:
                for base, size in enum_regions(h):
                    off = 0
                    while off < size:
                        sz = min(8 * 1024 * 1024, size - off)
                        data = read_mem(h, base + off, sz)
                        off += sz
                        if not data:
                            continue
                        n = len(data) - 31
                        i = 0
                        while i < n:
                            # 恰好 32 字符字母数字 run（ASCII + UTF-16LE 两种）
                            if data[i] in HEXCH:
                                if all(data[i + j] in HEXCH for j in range(1, 32)):
                                    key = data[i:i + 16]
                                    if verify_aes_key(key, ciphertext):
                                        if progress:
                                            progress(f"  !!! 命中 key: {key.decode('ascii')}")
                                        return key.decode("ascii")
                                    i += 32
                                else:
                                    i += 1
                            else:
                                i += 1
            finally:
                kernel32.CloseHandle(h)
        if progress:
            progress(f"  [i] 轮询中... 剩余 {int(deadline - time.time())}s（请打开微信任意一张图片查看）")
        time.sleep(interval)
    return None


# ---------------------------------------------------------------- WXGF 转码

class WxAMConfig(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_int), ("reserved", ctypes.c_int)]


def find_voip_engine_dll(install_dir=None):
    """找微信安装目录的 VoipEngine.dll（WXGF 解码器）。"""
    cands = []
    if install_dir:
        cands.append(install_dir)
    # 从运行中的 Weixin.exe 定位安装目录
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Name Weixin -ErrorAction SilentlyContinue | "
             "Select-Object -First 1 -ExpandProperty Path)"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if out and os.path.isfile(out):
            cands.append(os.path.dirname(out))
    except Exception:
        pass
    for c in cands:
        for root, _d, files in os.walk(c):
            if "VoipEngine.dll" in files:
                return os.path.join(root, "VoipEngine.dll")
            # 微信目录通常不深，限制层级避免全盘
    return None


def wxgf_to_image(data: bytes, dll_path: str) -> bytes | None:
    """WXGF 容器转码为 jpg（微信自带 WxAM 解码器）。"""
    try:
        dll = ctypes.WinDLL(dll_path)
        fn = dll.wxam_dec_wxam2pic_5
        fn.argtypes = [ctypes.c_int64, ctypes.c_int, ctypes.c_int64,
                       ctypes.POINTER(ctypes.c_int), ctypes.c_int64]
        fn.restype = ctypes.c_int64
    except Exception:
        return None
    max_out = 52 * 1024 * 1024
    for mode in (0, 3):
        try:
            config = WxAMConfig()
            config.mode = mode
            config.reserved = 0
            in_buf = ctypes.create_string_buffer(data, len(data))
            out_buf = ctypes.create_string_buffer(max_out)
            out_size = ctypes.c_int(max_out)
            result = fn(ctypes.addressof(in_buf), len(data),
                        ctypes.addressof(out_buf), ctypes.byref(out_size),
                        ctypes.addressof(config))
            if result != 0 or out_size.value <= 0:
                continue
            out = out_buf.raw[: out_size.value]
            if out[:3] == b"\xff\xd8\xff" or out[:8] == b"\x89PNG\r\n\x1a\n":
                return out
        except Exception:
            continue
    return None
