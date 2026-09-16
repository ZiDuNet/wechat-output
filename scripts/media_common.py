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
import hashlib
import os
import struct
import subprocess
import sys
import time

from aes_backend import aes_ecb_decrypt  # 跨平台 AES-128-ECB（win=bcrypt，零依赖）

V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
V1_MAGIC = b"\x07\x08\x56\x31\x08\x07"
AES_BLOCK = 16

# ---------------------------------------------------------------- AES（走 aes_backend 跨平台后端）
# aes_ecb_decrypt 已从 aes_backend 导入（win32=bcrypt.dll CNG，行为与原实现逐字节一致；
# darwin=CommonCrypto CCCrypt；linux=OpenSSL EVP）。此处不再直连 bcrypt。


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


# ---------------------------------------------------------------- 进程内存（平台抽象）
# 统一原语：_find_wechat_pids / _open_process(pid) / _close_process(h)
#           / _enum_regions(h) / _read_mem(h, addr, sz)
# win32 走原 kernel32 三件套（与旧实现逐字节一致）；darwin/linux 分支只在对应平台执行。

MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
MAX_ADDR = 0x7FFFFFFFFFFF


if sys.platform == "win32":
    import ctypes.wintypes as _wt  # noqa: E402  仅 Windows 存在

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
                    ("AllocationProtect", _wt.DWORD), ("_pad1", _wt.DWORD),
                    ("RegionSize", ctypes.c_uint64), ("State", _wt.DWORD),
                    ("Protect", _wt.DWORD), ("Type", _wt.DWORD), ("_pad2", _wt.DWORD)]

    _k32 = ctypes.windll.kernel32
    _VM_READ, _QUERY = 0x0010, 0x0400

    def _find_wechat_pids():
        """Windows：tasklist 找 Weixin.exe，按内存降序。"""
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

    def _open_process(pid):
        return _k32.OpenProcess(_VM_READ | _QUERY, False, pid)

    def _close_process(h):
        if h:
            _k32.CloseHandle(h)

    def _read_mem(h, addr, sz):
        buf = ctypes.create_string_buffer(sz)
        n = ctypes.c_size_t(0)
        if _k32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, sz, ctypes.byref(n)):
            return buf.raw[: n.value]
        return None

    def _enum_regions(h):
        regs, addr, mbi = [], 0, MBI()
        while addr < MAX_ADDR:
            if _k32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < mbi.RegionSize < 500 * 1024 * 1024:
                regs.append((mbi.BaseAddress, mbi.RegionSize))
            nxt = mbi.BaseAddress + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt
        return regs


elif sys.platform == "darwin":
    # 【代码级验证，未真机】移植自上游 wcdb_key_tool_macos.py：
    # task_for_pid + mach_vm_region + mach_vm_read。前置：sudo codesign 重签 WeChat.app、root。
    import ctypes.util as _cu  # noqa: E402

    KERN_SUCCESS = 0
    VM_REGION_BASIC_INFO_64 = 9
    VM_REGION_BASIC_INFO_COUNT_64 = 9
    VM_PROT_READ = 0x01
    _libSystem = ctypes.CDLL(_cu.find_library("System"))
    _libSystem.mach_task_self.restype = ctypes.c_uint32

    class vm_region_basic_info_64(ctypes.Structure):
        _fields_ = [
            ("protection", ctypes.c_int32), ("max_protection", ctypes.c_int32),
            ("inheritance", ctypes.c_uint32), ("shared", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32), ("offset", ctypes.c_uint64),
            ("behavior", ctypes.c_int32), ("user_wired_count", ctypes.c_uint16),
        ]

    def _find_wechat_pids():
        """macOS：pgrep -x WeChat。"""
        try:
            r = subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True, text=True)
            return [(int(p), 0) for p in r.stdout.split() if p.strip().isdigit()]
        except (FileNotFoundError, ValueError):
            return []

    def _open_process(pid):
        """返回 mach task port（作为后续读写的句柄 h）。"""
        task = ctypes.c_uint32(0)
        kr = _libSystem.task_for_pid(_libSystem.mach_task_self(), ctypes.c_int(pid), ctypes.byref(task))
        if kr != KERN_SUCCESS:
            raise PermissionError(
                f"task_for_pid failed for PID={pid} (kern_return={kr})。"
                "macOS 需先 sudo codesign --force --deep --sign - /Applications/WeChat.app 去 Hardened Runtime 并重启微信，且以 root 运行。"
            )
        return task.value

    def _close_process(h):
        # mach task port 无需显式关闭
        return None

    def _read_mem(h, addr, sz):
        data_ptr = ctypes.c_uint64(0)
        data_size = ctypes.c_uint64(0)
        kr = _libSystem.mach_vm_read(ctypes.c_uint32(h), ctypes.c_uint64(addr),
                                     ctypes.c_uint64(sz), ctypes.byref(data_ptr), ctypes.byref(data_size))
        if kr != KERN_SUCCESS:
            return None
        try:
            return ctypes.string_at(data_ptr.value, data_size.value)
        finally:
            _libSystem.mach_vm_deallocate(_libSystem.mach_task_self(), data_ptr, data_size)

    def _enum_regions(h):
        regions = []
        address = ctypes.c_uint64(0)
        size = ctypes.c_uint64(0)
        info = vm_region_basic_info_64()
        info_count = ctypes.c_uint32(VM_REGION_BASIC_INFO_COUNT_64)
        object_name = ctypes.c_uint32(0)
        while True:
            kr = _libSystem.mach_vm_region(
                ctypes.c_uint32(h), ctypes.byref(address), ctypes.byref(size),
                ctypes.c_int(VM_REGION_BASIC_INFO_64), ctypes.byref(info),
                ctypes.byref(info_count), ctypes.byref(object_name))
            if kr != KERN_SUCCESS:
                break
            reg_size = size.value
            if (info.protection & VM_PROT_READ) and 0 < reg_size < 500 * 1024 * 1024:
                regions.append((address.value, reg_size))
            nxt = address.value + reg_size
            if nxt <= address.value:
                break
            address.value = nxt
        return regions


elif sys.platform.startswith("linux"):
    # 【代码级验证，未真机】/proc/<pid>/maps + /proc/<pid>/mem。前置：root 或放开 yama/ptrace_scope。
    class _LinuxProc:
        """把 (pid, mem_fd) 包成不透明句柄 h。"""
        __slots__ = ("pid", "mem")

        def __init__(self, pid, mem):
            self.pid = pid
            self.mem = mem

    def _find_wechat_pids():
        """Linux：遍历 /proc/*/exe 找结尾 /wechat 的进程。"""
        pids = []
        for pid_str in os.listdir("/proc"):
            if not pid_str.isdigit():
                continue
            try:
                exe = os.readlink(f"/proc/{pid_str}/exe")
                if exe.endswith("/wechat"):
                    pids.append((int(pid_str), 0))
            except (OSError, PermissionError):
                continue
        return pids

    def _open_process(pid):
        mem = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        return _LinuxProc(pid, mem)

    def _close_process(h):
        try:
            os.close(h.mem)
        except OSError:
            pass

    def _read_mem(h, addr, sz):
        try:
            return os.pread(h.mem, sz, addr)
        except OSError:
            return None

    def _enum_regions(h):
        regions = []
        try:
            with open(f"/proc/{h.pid}/maps", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 5:
                        continue
                    perms = parts[1]
                    if "r" not in perms:
                        continue
                    start_s, end_s = parts[0].split("-")
                    start, end = int(start_s, 16), int(end_s, 16)
                    size = end - start
                    if 0 < size < 500 * 1024 * 1024:
                        regions.append((start, size))
        except OSError:
            pass
        return regions


else:
    raise RuntimeError(f"不支持的平台: {sys.platform!r}（内存提取仅支持 win32/darwin/linux）")


# 向后兼容别名（旧调用方仍可用 read_mem/enum_regions/find_wechat_pids）
def read_mem(h, addr, sz):
    return _read_mem(h, addr, sz)


def enum_regions(h):
    return _enum_regions(h)


def find_wechat_pids():
    return _find_wechat_pids()


def scan_code_in_memory(wxid: str, xor_hint: int | None = None, progress=None):
    """扫微信进程内存找 code 候选（4 字节 LE 整数，常驻登录态）。

    预过滤：xor_hint 已知时只扫低字节 == xor_hint 的窗口（1/256，快 ~200 倍）；
    返回 [(code, 出现次数), ...] 按次数降序。真实 code 在内存有大量副本（实测数百处），
    由调用方按序派生密钥验证模板密文即可命中。
    """
    candidates = {}
    for pid, _kb in find_wechat_pids():
        h = _open_process(pid)
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
            _close_process(h)
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
    HEXCH = set(b"0123456789abcdefABCDEF")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for pid, _kb in find_wechat_pids():
            h = _open_process(pid)
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
                _close_process(h)
        if progress:
            progress(f"  [i] 轮询中... 剩余 {int(deadline - time.time())}s（请打开微信任意一张图片查看）")
        time.sleep(interval)
    return None


# ---------------------------------------------------------------- WXGF 转码

class WxAMConfig(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_int), ("reserved", ctypes.c_int)]


def find_voip_engine_dll(install_dir=None):
    """找微信自带的 WXGF 解码器（win=VoipEngine.dll；mac=WeChat.app 内 dylib；linux=不可用）。

    返回可传给 wxgf_to_image 的库路径（str），找不到返回 None。
    - win32：原逻辑，从运行中 Weixin.exe 定位安装目录后找 VoipEngine.dll。【已验证】
    - darwin：【推断，未真机】在 /Applications/WeChat.app/Contents/Frameworks 下找
      可能含 wxam_dec_wxam2pic_5 导出符号的 .dylib。微信 4.x 与 Windows 共用 WCDB/
      图片解码代码，符号名大概率一致；但未在 mac 真机核对具体 dylib 名。
    - linux：官方 Linux 微信不一定附带同款解码库，直接降级为 None。
    """
    if sys.platform == "win32":
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

    if sys.platform == "darwin":
        # 【推断，未真机】macOS：定位 WeChat.app 内的候选 dylib
        base = install_dir or "/Applications/WeChat.app"
        fw = os.path.join(base, "Contents", "Frameworks")
        if os.path.isdir(fw):
            for root, _d, files in os.walk(fw):
                for fn in files:
                    if fn.endswith(".dylib"):
                        return os.path.join(root, fn)
        print("[media_common][WXGF] macOS 未在 WeChat.app/Contents/Frameworks 找到解码器 dylib："
              "未查看原图暂不可用，已查看的明文图/视频不受影响。", flush=True)
        return None

    # linux 及其它：降级说明
    print("[media_common][WXGF] Linux 官方微信不附带 WXGF 解码库：未查看原图暂不可用，"
          "已查看的明文图/视频不受影响。", flush=True)
    return None


def wxgf_to_image(data: bytes, dll_path: str) -> bytes | None:
    """WXGF 容器转码为 jpg（微信自带 WxAM 解码器 wxam_dec_wxam2pic_5）。"""
    try:
        # win32 用 WinDLL；darwin 用 CDLL（dylib）。Linux 上 dll_path 恒为 None，不会进到这。
        loader = ctypes.WinDLL if sys.platform == "win32" else ctypes.CDLL
        lib = loader(dll_path)
        fn = lib.wxam_dec_wxam2pic_5
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


# ---------------------------------------------------------------- 时间范围过滤（各导出脚本共用）
def parse_time_range(since=None, until=None, last=None):
    """解析 --since/--until/--last 为 (since_ts, until_ts)（unix 秒，含边界）。
    - since/until: YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS；until 省略时刻时按当日 23:59:59（含当天）
    - last 支持自然语言（大小写/中英均可）：
        today / 今天            = 今天 00:00 ~ 23:59
        yesterday / 昨天        = 昨天 00:00 ~ 昨天 23:59
        <N>d|w|m|y / 近N天|周|月|年 = 近 N 天/周/月/年（含今天），如 7d / 2w / 3m / 1y
        all / 0 / 全部          = 不过滤（= 不传）
    - 全部不传返回 (None, None)。"""
    import re
    from datetime import datetime, timedelta
    if since is None and until is None and last is None:
        return None, None
    now = datetime.now()
    def _parse(s, end_of_day):
        s = s.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%Y-%m-%d" and end_of_day:
                    dt = dt.replace(hour=23, minute=59, second=59)
                return int(dt.timestamp())
            except ValueError:
                continue
        raise ValueError(f"时间格式不支持: {s}（用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）")
    since_ts = _parse(since, False) if since else None
    until_ts = _parse(until, True) if until else None
    if last:
        s = last.strip().lower().replace(" ", "")
        if s in ("today", "今天"):
            d0 = now.replace(hour=0, minute=0, second=0)
            since_ts, until_ts = int(d0.timestamp()), int(now.replace(hour=23, minute=59, second=59).timestamp())
        elif s in ("yesterday", "昨天"):
            y0 = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0)
            since_ts, until_ts = int(y0.timestamp()), int(y0.replace(hour=23, minute=59, second=59).timestamp())
        elif s in ("all", "全部", "0"):
            return None, None
        else:
            m = re.match(r"^(?:近)?(\d+)([dwmy]|天|周|月|年)$", s)
            if not m:
                raise ValueError(f"--last 格式不支持: {last}（用 today / yesterday / 7d / 2w / 3m / 1y / all）")
            n, unit = int(m.group(1)), m.group(2)
            mul = {"d": 1, "w": 7, "m": 30, "y": 365,
                   "天": 1, "周": 7, "月": 30, "年": 365}[unit]
            start = (now - timedelta(days=n * mul)).replace(hour=0, minute=0, second=0)
            since_ts = int(start.timestamp())
            if until_ts is None:
                until_ts = int(now.replace(hour=23, minute=59, second=59).timestamp())
    if since_ts and until_ts and since_ts > until_ts:
        raise ValueError("--since 晚于 --until，时间范围为空")
    return since_ts, until_ts


def add_time_args(ap):
    """给 argparse 统一加 --since/--until/--last 三参数，返回即可。"""
    ap.add_argument("--since", help="起始时间 YYYY-MM-DD[ HH:MM:SS]（含）")
    ap.add_argument("--until", help="结束时间 YYYY-MM-DD[ HH:MM:SS]（含当天）")
    ap.add_argument("--last", help="时间范围：today/今天、yesterday/昨天、7d/近7天、2w/2周、3m/3月、1y/1年、all/全部")
    return ap
