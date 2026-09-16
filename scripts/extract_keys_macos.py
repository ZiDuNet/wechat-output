#!/usr/bin/env python3
"""微信数据库密钥提取（macOS）—— 跨平台移植版

【代码级验证，未真机】本脚本只在 Windows 开发机上做过语法/import/逻辑走查，
未在 macOS 真机跑过。逻辑逐行移植自上游 TANGandXue/wcdb-key-tool 的
``wcdb_key_tool_macos.py``（MIT，已在 mac 真机验证），并按本仓库"一键入口"风格整合：

- CLI 与 ``wcdb_key_tool_windows.py`` 对齐：``extract`` / ``decrypt`` 两个子命令，
  ``--db-dir`` / ``--keys`` / ``--output`` / ``--out`` 参数一致，便于 ``wx_export.py``
  在 darwin 上无缝切换。
- AES-CBC 走本仓库 ``aes_backend``（darwin=CommonCrypto CCCrypt），与上层解密零分叉。
- 密钥缓存沿用 ``~/.wxcache/all_keys.json`` 结构：{<相对路径>: {enc_key, salt, size_mb}, "_db_dir": ...}。

macOS 专属前置（每次微信自动更新后可能要重做一次）：
    sudo codesign --force --deep --sign - /Applications/WeChat.app   # 去 Hardened Runtime，否则 task_for_pid 被拒
    xcode-select --install                                          # 提供 lldb
    sudo python3 extract_keys_macos.py extract                       # task_for_pid / lldb attach 需要 root
首次抓 passphrase 需在微信内「退出登录 → 重新登录」触发派生计算。

【可选增强】Frida 备选抓取路线（非必装，参考 yichen-wechat-local-vault hook CCKeyDerivationPBKDF 思路）：
    pip install frida frida-tools
    装了就优先用 frida hook，没装或失败一律回落默认 LLDB 路线（try-import 包裹，不强制）。

用法:
    sudo python3 extract_keys_macos.py extract [--db-dir ...] [--out all_keys.json] [--decrypt]
    sudo python3 extract_keys_macos.py decrypt  [--db-dir ...] --keys all_keys.json --output decrypted

上游来源: https://github.com/TANGandXue/wcdb-key-tool  (MIT)
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import hmac as hmac_mod
import json
import os
import pathlib
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import time

# 复用本仓库跨平台 AES 后端（darwin=CommonCrypto）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aes_backend import aes_cbc_decrypt  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

_print = lambda *a, **kw: print(*a, **kw)  # noqa: E731

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
SQLITE_HDR = b"SQLite format 3\x00"
PASSPHRASE_FILE = os.path.join(os.path.expanduser("~"), ".wcdb-key-tool", "wechat-passphrase.json")

# 【代码级验证，未真机】以下 Mach 调用仅在 darwin 执行；Windows 上 import 不会进到这
_KERN_SUCCESS = 0
_VM_REGION_BASIC_INFO_64 = 9
_VM_REGION_BASIC_INFO_COUNT_64 = 9
_VM_PROT_READ = 0x01
_WECHAT_KEY_PATTERN = re.compile(rb"x'([0-9a-f]{96})'")

_libSystem = None
if sys.platform == "darwin":
    _libSystem = ctypes.CDLL(ctypes.util.find_library("System"))


class vm_region_basic_info_64(ctypes.Structure):
    _fields_ = [
        ("protection", ctypes.c_int32), ("max_protection", ctypes.c_int32),
        ("inheritance", ctypes.c_uint32), ("shared", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32), ("offset", ctypes.c_uint64),
        ("behavior", ctypes.c_int32), ("user_wired_count", ctypes.c_uint16),
    ]


class SignatureInvalidError(RuntimeError):
    """所有微信进程 task_for_pid 失败（签名被系统还原），需要重新 codesign。"""


class KeysNotFoundError(RuntimeError):
    """能读内存但扫不到密钥（微信未登录，或 4.1.10+ 不再缓存明文密钥）。"""


class CaptureError(RuntimeError):
    pass


# ============================================================
# 校验 / 收集（纯 Python，跨平台一致）
# ============================================================
def verify_enc_key(enc_key: bytes, db_page1: bytes) -> bool:
    salt = db_page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - 80 + 16]
    stored_hmac = db_page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def collect_db_files(db_dir: str):
    db_files, salt_to_dbs = [], {}
    for root, _dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                continue
            path = os.path.join(root, name)
            size = os.path.getsize(path)
            if size < PAGE_SZ:
                continue
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
            rel = os.path.relpath(path, db_dir)
            salt = page1[:SALT_SZ].hex()
            db_files.append((rel, path, size, salt, page1))
            salt_to_dbs.setdefault(salt, []).append(rel)
    return db_files, salt_to_dbs


# ============================================================
# 内存扫 raw key（微信 4.0.x）—— task_for_pid + mach_vm_read
# ============================================================
def _task_for_pid(pid: int) -> int:
    task = ctypes.c_uint32(0)
    kr = _libSystem.task_for_pid(_libSystem.mach_task_self(), ctypes.c_int(pid), ctypes.byref(task))
    if kr != _KERN_SUCCESS:
        raise PermissionError(
            f"task_for_pid failed for PID={pid} (kern_return={kr})。"
            "请先 sudo codesign --force --deep --sign - /Applications/WeChat.app 去 Hardened Runtime 并重启微信。")
    return task.value


def _enum_readable_regions(task: int):
    regions = []
    address = ctypes.c_uint64(0)
    size = ctypes.c_uint64(0)
    info = vm_region_basic_info_64()
    info_count = ctypes.c_uint32(_VM_REGION_BASIC_INFO_COUNT_64)
    object_name = ctypes.c_uint32(0)
    while True:
        kr = _libSystem.mach_vm_region(
            ctypes.c_uint32(task), ctypes.byref(address), ctypes.byref(size),
            ctypes.c_int(_VM_REGION_BASIC_INFO_64), ctypes.byref(info),
            ctypes.byref(info_count), ctypes.byref(object_name))
        if kr != _KERN_SUCCESS:
            break
        reg_size = size.value
        if (info.protection & _VM_PROT_READ) and 0 < reg_size < 500 * 1024 * 1024:
            regions.append((address.value, reg_size))
        nxt = address.value + reg_size
        if nxt <= address.value:
            break
        address.value = nxt
    return regions


def _read_memory(task: int, address: int, size: int):
    data_ptr = ctypes.c_uint64(0)
    data_size = ctypes.c_uint64(0)
    kr = _libSystem.mach_vm_read(ctypes.c_uint32(task), ctypes.c_uint64(address),
                                 ctypes.c_uint64(size), ctypes.byref(data_ptr), ctypes.byref(data_size))
    if kr != _KERN_SUCCESS:
        return None
    try:
        return ctypes.string_at(data_ptr.value, data_size.value)
    finally:
        _libSystem.mach_vm_deallocate(_libSystem.mach_task_self(), data_ptr, data_size)


def _find_pids(process_name: str = "WeChat"):
    try:
        r = subprocess.run(["pgrep", "-x", process_name], capture_output=True, text=True)
        return [int(p) for p in r.stdout.strip().split() if p.strip().isdigit()]
    except (FileNotFoundError, ValueError):
        return []


def _scan_memory_raw_key(db_dir: str, keys_file: str) -> dict:
    db_files, salt_to_dbs = collect_db_files(db_dir)
    if not db_files:
        raise RuntimeError(f"在 {db_dir} 未找到可解密的 .db 文件")
    _print(f"找到 {len(db_files)} 个数据库, {len(salt_to_dbs)} 个不同的 salt")

    pids = _find_pids("WeChat")
    if not pids:
        raise RuntimeError("未找到微信进程，请先启动微信")
    _print(f"找到微信进程: {pids}")

    key_map, remaining = {}, set(salt_to_dbs.keys())
    task_ok = 0
    for pid in pids:
        if not remaining:
            break
        _print(f"\n[*] 扫描 PID={pid}")
        try:
            task = _task_for_pid(pid)
            task_ok += 1
        except PermissionError as e:
            _print(f"[WARN] {e}")
            continue
        for base, size in _enum_readable_regions(task):
            if not remaining:
                break
            data = _read_memory(task, base, size)
            if not data:
                continue
            for m in _WECHAT_KEY_PATTERN.finditer(data):
                hex_str = m.group(1).decode()
                enc_key_hex, salt_hex = hex_str[:64], hex_str[64:]
                if salt_hex not in remaining:
                    continue
                enc_key = bytes.fromhex(enc_key_hex)
                for _rel, _p, _sz, s, page1 in db_files:
                    if s == salt_hex and verify_enc_key(enc_key, page1):
                        key_map[salt_hex] = enc_key_hex
                        remaining.discard(salt_hex)
                        _print(f"  [FOUND] salt={salt_hex} enc_key={enc_key_hex}")
                        break
    if not key_map:
        if task_ok == 0:
            raise SignatureInvalidError("所有微信进程都无法读内存（task_for_pid 失败），需重新 codesign。")
        raise KeysNotFoundError("能读内存但未扫到密钥（微信未登录，或已是不再缓存明文密钥的新版本）")
    _save_results(db_files, salt_to_dbs, key_map, db_dir, keys_file)
    return key_map


# ============================================================
# LLDB 断点抓 passphrase（微信 4.1.10+：CCKeyDerivationPBKDF）
# ============================================================
DEFAULT_TIMEOUT = 180


# ----------------------------------------------------------------
# 可选增强：Frida hook CCKeyDerivationPBKDF（非必装，参考 yichen-wechat-local-vault 思路）
#   pip install frida frida-tools   # 仅在 macOS 上可选；不装则走默认 LLDB/cTypes 路线
# 【可选增强，未真机】本仓库未在 mac 真机验证 Frida 路线；仅做 try-import 包裹，
#   装了就提示可用并尝试，没装或失败一律回落到默认 LLDB 路线。
# ----------------------------------------------------------------
try:
    import frida  # noqa: F401
    _FRIDA_AVAILABLE = True
except Exception:
    _FRIDA_AVAILABLE = False


_FRIDA_SCRIPT = r"""
(function () {
  var sym = null;
  try {
    sym = Module.getExportByName(null, 'CCKeyDerivationPBKDF');
  } catch (e) {}
  if (!sym) { send({type: 'error', payload: '未找到 CCKeyDerivationPBKDF 导出符号'}); return; }
  Interceptor.attach(sym, {
    onEnter: function (args) {
      // CCKeyDerivationPBKDF(alg, password, passwordLen, ...)：args[1]=password, args[2]=passwordLen
      try {
        var len = args[2].toInt32();
        if (len === 32) {
          send({type: 'passphrase', payload: args[1].readByteArray(32)});
        }
      } catch (e) {}
    }
  });
  send({type: 'ready'});
})();
"""


def capture_passphrase_frida(timeout: int = DEFAULT_TIMEOUT) -> str:
    """【可选增强，未真机】用 frida attach 微信进程，hook CCKeyDerivationPBKDF 抓 32B passphrase。

    需要用户在捕获期间退出登录再重新登录触发派生。仅当本机装了 frida 才会被调用。
    """
    if not _FRIDA_AVAILABLE:
        raise CaptureError("未安装 frida（pip install frida frida-tools）")
    pids = _find_pids("WeChat")
    if not pids:
        raise CaptureError("未找到微信进程，请先启动并登录微信")
    import frida as _frida

    result: dict = {}

    def _on_message(message, data):
        if message.get("type") == "send":
            payload = message.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "passphrase":
                raw = payload.get("payload")
                if isinstance(raw, bytes):
                    result["hex"] = raw.hex()
        elif message.get("type") == "error":
            result["error"] = message.get("description", str(message))

    session = _frida.attach(pids[0])
    script = session.create_script(_FRIDA_SCRIPT)
    script.on("message", _on_message)
    script.load()

    _print("[frida] 已 attach 并 hook CCKeyDerivationPBKDF，请在微信内退出登录再重新登录...")
    deadline = time.time() + timeout
    while time.time() < deadline and "hex" not in result:
        if "error" in result:
            session.detach()
            raise CaptureError(f"frida hook 出错: {result['error']}")
        time.sleep(1.0)
    session.detach()
    if "hex" in result:
        return result["hex"]
    raise CaptureError("frida 路线超时未抓到 passphrase（请确认已重新登录微信）")


def check_lldb_prerequisites() -> list[str]:
    issues = []
    if not shutil.which("lldb"):
        issues.append("未检测到 lldb，请先: xcode-select --install")
    return issues


def _parse_passphrase(out: str):
    by = []
    for line in out.splitlines():
        m = re.match(r"\s*0x[0-9a-f]+:\s+((?:0x[0-9a-f]{2}\s*)+)$", line)
        if m:
            by += re.findall(r"0x([0-9a-f]{2})", m.group(1))
    return "".join(by[:32]) if len(by) >= 32 else None


def capture_passphrase_lldb(timeout: int = DEFAULT_TIMEOUT) -> str:
    import platform
    issues = check_lldb_prerequisites()
    if issues:
        raise CaptureError("; ".join(issues))
    pids = _find_pids("WeChat")
    if not pids:
        raise CaptureError("未找到微信进程，请先启动并登录微信")
    pid = pids[0]
    is_arm = platform.machine() in ("arm64", "aarch64")
    pw_reg, len_reg = ("x1", "x2") if is_arm else ("rsi", "rdx")

    script = (
        "settings set target.preload-symbols false\n"
        f"process attach -p {pid}\n"
        f"breakpoint set -n CCKeyDerivationPBKDF -c '${len_reg} == 32'\n"
        "breakpoint command add 1\n"
        f"memory read --size 1 --count 32 --format x ${pw_reg}\n"
        "detach\nquit\nDONE\nprocess continue\n"
    )
    fd, sp = tempfile.mkstemp(suffix=".lldb", prefix="wxcap_")
    with os.fdopen(fd, "w") as f:
        f.write(script)
    proc = subprocess.Popen(
        ["bash", "-c", f"( cat {sp}; sleep {timeout} ) | TERM=dumb lldb"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    buf = ""
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            r, _, _ = select.select([proc.stdout], [], [], 3.0)
            if r:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                buf += line
            if _parse_passphrase(buf):
                break
    finally:
        try:
            proc.terminate(); proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.unlink(sp)
        except OSError:
            pass
    ph = _parse_passphrase(buf)
    if ph:
        return ph
    raise CaptureError("未捕获到 passphrase，请确认捕获期间在微信内退出登录并重新登录。")


def load_passphrase():
    try:
        with open(PASSPHRASE_FILE, "r") as f:
            return json.load(f).get("passphrase")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def save_passphrase(passphrase: str):
    os.makedirs(os.path.dirname(PASSPHRASE_FILE), exist_ok=True)
    with open(PASSPHRASE_FILE, "w") as f:
        json.dump({"passphrase": passphrase}, f, indent=2)
    os.chmod(PASSPHRASE_FILE, 0o600)
    _print(f"passphrase 已保存到 {PASSPHRASE_FILE}")


def _derive_keys_from_passphrase(passphrase: bytes, db_files, salt_to_dbs) -> dict:
    key_map = {}
    total = len(salt_to_dbs)
    for i, salt_hex in enumerate(salt_to_dbs):
        salt = bytes.fromhex(salt_hex)
        enc_key = hashlib.pbkdf2_hmac("sha512", passphrase, salt, 256000, dklen=KEY_SZ)
        for _rel, _p, _sz, s, page1 in db_files:
            if s == salt_hex and verify_enc_key(enc_key, page1):
                key_map[salt_hex] = enc_key.hex()
                break
        if (i + 1) % 5 == 0 or i == total - 1:
            _print(f"  PBKDF2 派生: {i + 1}/{total} ({len(key_map)} 验证通过)")
    return key_map


# ============================================================
# 密钥 JSON 落盘 + 路径分隔符归一
# ============================================================
def _strip_key_metadata(keys: dict) -> dict:
    return {k: v for k, v in keys.items() if not k.startswith("_")}


def _key_path_variants(rel_path: str):
    normalized = rel_path.replace("\\", "/")
    variants = []
    for candidate in (rel_path, normalized, normalized.replace("/", "\\"), normalized.replace("/", os.sep)):
        if candidate not in variants:
            variants.append(candidate)
    return variants


def _get_key_info(keys: dict, rel_path: str):
    if ".." in rel_path.replace("\\", "/").split("/"):
        return None
    for candidate in _key_path_variants(rel_path):
        if candidate in keys and not candidate.startswith("_"):
            return keys[candidate]
    return None


def _save_results(db_files, salt_to_dbs, key_map, db_dir, out_file):
    _print(f"\n{'=' * 60}\n结果: {len(key_map)}/{len(salt_to_dbs)} salts 找到密钥")
    result = {}
    for rel, _p, sz, salt_hex, _page1 in db_files:
        if salt_hex in key_map:
            result[rel] = {"enc_key": key_map[salt_hex], "salt": salt_hex,
                           "size_mb": round(sz / 1024 / 1024, 1)}
            _print(f"  OK: {rel} ({sz / 1024 / 1024:.1f}MB)")
        else:
            _print(f"  MISSING: {rel} (salt={salt_hex})")
    if not result:
        raise RuntimeError("未能提取到任何密钥")
    result["_db_dir"] = db_dir
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    _print(f"\n密钥保存到: {out_file}")


# ============================================================
# 数据目录探测（macOS 4.x 沙盒容器）
# ============================================================
def auto_detect_db_dir():
    container_root = os.path.expanduser(
        "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")
    if not os.path.isdir(container_root):
        return None
    candidates = []
    for entry in os.listdir(container_root):
        db_storage = os.path.join(container_root, entry, "db_storage")
        if os.path.isdir(db_storage):
            candidates.append(db_storage)
    for candidate in candidates:
        for _ in pathlib.Path(candidate).rglob("*.db"):
            return candidate
    return candidates[0] if candidates else None


# ============================================================
# 解密（AES 走 aes_backend）
# ============================================================
def _decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        return bytes(SQLITE_HDR + aes_cbc_decrypt(enc_key, iv, encrypted) + b"\x00" * RESERVE_SZ)
    encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
    return aes_cbc_decrypt(enc_key, iv, encrypted) + b"\x00" * RESERVE_SZ


def _decrypt_database(db_path: str, out_path: str, enc_key: bytes) -> bool:
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ
    if file_size % PAGE_SZ != 0:
        _print(f"  [WARN] 文件大小 {file_size} 不是 {PAGE_SZ} 的倍数")
        total_pages += 1
    with open(db_path, "rb") as fin:
        page1 = fin.read(PAGE_SZ)
    if len(page1) < PAGE_SZ:
        return False
    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    p1_hmac_data = page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    p1_stored_hmac = page1[PAGE_SZ - HMAC_SZ: PAGE_SZ]
    hm = hmac_mod.new(mac_key, p1_hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    if hm.digest() != p1_stored_hmac:
        _print(f"  [ERROR] Page 1 HMAC 验证失败! salt: {salt.hex()}")
        return False
    _print(f"  HMAC OK, {total_pages} pages")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(db_path, "rb") as fin, open(out_path, "wb") as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page += b"\x00" * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(_decrypt_page(enc_key, page, pgno))
    return True


def decrypt_all(db_dir: str, out_dir: str, keys_file: str) -> dict:
    _print("=" * 60 + "\n  WeChat 数据库解密器 (macOS)\n" + "=" * 60)
    if not os.path.exists(keys_file):
        _print(f"[ERROR] 密钥文件不存在: {keys_file}")
        sys.exit(1)
    with open(keys_file, encoding="utf-8") as f:
        keys = _strip_key_metadata(json.load(f))
    _print(f"\n加载 {len(keys)} 个数据库密钥\n输出目录: {out_dir}")
    os.makedirs(out_dir, exist_ok=True)
    db_files = []
    for root, _d, files in os.walk(db_dir):
        for fname in files:
            if fname.endswith(".db") and not fname.endswith("-wal") and not fname.endswith("-shm"):
                db_files.append((fname, os.path.join(root, fname), os.path.getsize(os.path.join(root, fname))))
    db_files.sort(key=lambda x: x[2])
    success = failed = 0
    for rel, path, sz in db_files:
        key_info = _get_key_info(keys, rel)
        if not key_info:
            failed += 1
            continue
        out_path = os.path.join(out_dir, rel)
        _print(f"解密: {rel} ({sz / 1024 / 1024:.1f}MB) ...", end=" ")
        if _decrypt_database(path, out_path, bytes.fromhex(key_info["enc_key"])):
            _print("  OK")
            success += 1
        else:
            failed += 1
    _print(f"结果: {success} 成功, {failed} 失败, 共 {len(db_files)} 个")
    return {"success": success, "failed": failed, "total": len(db_files)}


# ============================================================
# CLI（与 wcdb_key_tool_windows.py 对齐：extract / decrypt）
# ============================================================
def cmd_extract(args):
    db_dir = args.db_dir or auto_detect_db_dir()
    if not db_dir:
        _print("[ERROR] 未能自动检测微信数据库目录，请用 --db-dir 指定")
        sys.exit(1)
    _print(f"[*] 数据库目录: {db_dir}")
    db_files, salt_to_dbs = collect_db_files(db_dir)
    if not db_files:
        _print(f"[ERROR] 在 {db_dir} 未找到可解密的 .db 文件")
        sys.exit(1)
    out_file = args.out

    # 第 1 级：缓存密钥
    if os.path.exists(out_file):
        try:
            existing = _strip_key_metadata(json.load(open(out_file, encoding="utf-8")))
            if all(_get_key_info(existing, rel) and
                   verify_enc_key(bytes.fromhex(_get_key_info(existing, rel)["enc_key"]), page1)
                   for rel, _p, _s, _salt, page1 in db_files):
                _print("[+] 已缓存密钥全部验证通过")
                if args.decrypt:
                    decrypt_all(db_dir, "decrypted", out_file)
                return
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # 第 2 级：已存 passphrase + PBKDF2
    passphrase_hex = load_passphrase()
    if passphrase_hex:
        _print("[*] 用已保存 passphrase 派生（PBKDF2）...")
        key_map = _derive_keys_from_passphrase(bytes.fromhex(passphrase_hex), db_files, salt_to_dbs)
        if key_map:
            _save_results(db_files, salt_to_dbs, key_map, db_dir, out_file)
            if args.decrypt:
                decrypt_all(db_dir, "decrypted", out_file)
            return

    # 第 3 级：内存扫 raw key（4.0.x）
    try:
        _scan_memory_raw_key(db_dir, out_file)
        if args.decrypt:
            decrypt_all(db_dir, "decrypted", out_file)
        return
    except KeysNotFoundError:
        _print("[*] 内存无明文密钥（4.1.10+），改走 LLDB 抓 passphrase")

    # 第 4 级：抓 passphrase（优先可选 Frida，回落默认 LLDB）
    ph = None
    if _FRIDA_AVAILABLE:
        _print("\n[*] 检测到 frida -> 优先尝试【可选增强】Frida hook 路线")
        try:
            ph = capture_passphrase_frida(timeout=args.timeout)
        except CaptureError as e:
            _print(f"[!] Frida 路线未成功（{e}），回落默认 LLDB 路线")
            ph = None
    else:
        _print("\n[i] 未安装 frida（pip install frida frida-tools）-> 走默认 LLDB 路线")
    if ph is None:
        _print("请在微信内：设置 → 退出登录 → 重新登录（触发派生），等待最多", args.timeout, "秒...")
        ph = capture_passphrase_lldb(timeout=args.timeout)
    save_passphrase(ph)
    key_map = _derive_keys_from_passphrase(bytes.fromhex(ph), db_files, salt_to_dbs)
    if not key_map:
        _print("[ERROR] PBKDF2 派生后未验证任何密钥")
        sys.exit(1)
    _save_results(db_files, salt_to_dbs, key_map, db_dir, out_file)
    if args.decrypt:
        decrypt_all(db_dir, "decrypted", out_file)


def cmd_decrypt(args):
    db_dir = args.db_dir
    if not db_dir and os.path.exists(args.keys):
        try:
            db_dir = json.load(open(args.keys, encoding="utf-8")).get("_db_dir")
        except Exception:
            pass
    db_dir = db_dir or auto_detect_db_dir()
    if not db_dir:
        _print("[ERROR] 未能确定数据库目录，请用 --db-dir 指定")
        sys.exit(1)
    decrypt_all(db_dir, args.output, args.keys)


def main():
    ap = argparse.ArgumentParser(description="微信数据库密钥提取（macOS）—— 跨平台移植版")
    sub = ap.add_subparsers(dest="command", required=True)
    e = sub.add_parser("extract", help="提取密钥（首次需重新登录微信）")
    e.add_argument("--db-dir"); e.add_argument("--out", "--output", dest="out", default="all_keys.json")
    e.add_argument("--decrypt", action="store_true"); e.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    d = sub.add_parser("decrypt", help="解密数据库（需已有密钥文件）")
    d.add_argument("--db-dir"); d.add_argument("--keys", default="all_keys.json")
    d.add_argument("--output", default="decrypted")
    args = ap.parse_args()
    if args.command == "extract":
        cmd_extract(args)
    else:
        cmd_decrypt(args)


if __name__ == "__main__":
    main()
