#!/usr/bin/env python3
"""微信数据库密钥提取（Linux，x86_64）—— 跨平台移植版

【代码级验证，未真机】本脚本只在 Windows 开发机上做过语法/import/逻辑走查，
未在 Linux 真机跑过；且上游 ELF 静态分析硬校验 x86_64，**ARM Linux 不支持**。
逻辑逐行移植自上游 TANGandXue/wcdb-key-tool 的 ``wcdb_key_tool.py``（MIT，已在 Linux x86_64
真机验证），按本仓库"一键入口"风格整合：CLI 与 ``wcdb_key_tool_windows.py`` 对齐，
AES-CBC 走本仓库 ``aes_backend``（linux=OpenSSL EVP），密钥缓存沿用 ``~/.wxcache/all_keys.json``。

Linux 专属前置：
    sudo apt install gdb libssl-dev
    # attach 需 root 或放开 ptrace:
    echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
首次抓 passphrase 需在微信内「退出登录 → 重新登录」触发 GDB 断点命中。

用法:
    sudo python3 extract_keys_linux.py extract [--db-dir ...] [--out all_keys.json] [--decrypt]
    sudo python3 extract_keys_linux.py decrypt [--db-dir ...] --keys all_keys.json --output decrypted

上游来源: https://github.com/TANGandXue/wcdb-key-tool  (MIT)
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import hashlib
import hmac as hmac_mod
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap

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

# ELF 锚点（仅 x86_64）
ANCHOR_STRING = b"com.Tencent.WCDB.Config.Cipher"
LEA_RSI = b"\x48\x8D\x35"
LEA_RDI = b"\x48\x8D\x3D"
FUNC_HEAD = b"\x55\x41\x57"
ELF_MAGIC = b"\x7fELF"
EM_X86_64 = 62


class CaptureError(RuntimeError):
    pass


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
# ELF 静态分析（仅 x86_64）
# ============================================================
class _ELFSection:
    __slots__ = ("name", "addr", "offset", "size", "data")

    def __init__(self, name, addr, offset, size, data):
        self.name, self.addr, self.offset, self.size, self.data = name, addr, offset, size, data


def _load_elf_sections(binary_path):
    data = pathlib.Path(binary_path).read_bytes()
    if data[:4] != ELF_MAGIC:
        raise RuntimeError(f"不是 ELF 文件: {binary_path}")
    if data[4] != 2 or data[5] != 1:
        raise RuntimeError(f"仅支持 ELF64 小端序: {binary_path}")
    (_e, machine, _v, _ent, _phoff, shoff, _fl, _ehs, _phes, _phn,
     shentsize, shnum, shstrndx) = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    if machine != EM_X86_64:
        raise RuntimeError("仅支持 x86_64 架构（ARM Linux 不在支持范围）")
    sections_raw = []
    for index in range(shnum):
        offset = shoff + index * shentsize
        vals = struct.unpack_from("<IIQQQQIIQQ", data, offset)[:6]
        sections_raw.append(vals)
    _, _, shstr_offset, shstr_size = sections_raw[shstrndx]
    shstr_data = data[shstr_offset: shstr_offset + shstr_size]
    sections = {}
    for sh_name, _t, _fl, sh_addr, sh_offset, sh_size in sections_raw:
        end = shstr_data.find(b"\0", sh_name)
        end = len(shstr_data) if end == -1 else end
        name = shstr_data[sh_name:end].decode("utf-8", errors="replace")
        if name:
            sections[name] = _ELFSection(name, sh_addr, sh_offset, sh_size,
                                         data[sh_offset: sh_offset + sh_size])
    return sections


def _find_rip_refs(text, opcode, target_va):
    hits = []
    for offset in range(max(0, len(text.data) - 7) + 1):
        if text.data[offset: offset + 3] != opcode:
            continue
        disp = struct.unpack_from("<i", text.data, offset + 3)[0]
        if text.addr + offset + 7 + disp == target_va:
            hits.append(offset)
    return hits


def find_hook_offset(binary_path) -> int:
    sections = _load_elf_sections(binary_path)
    rodata, text = sections[".rodata"], sections[".text"]
    candidates = []
    search_from = 0
    while True:
        anchor_offset = rodata.data.find(ANCHOR_STRING, search_from)
        if anchor_offset == -1:
            break
        search_from = anchor_offset + 1
        anchor_va = rodata.addr + anchor_offset
        for first in _find_rip_refs(text, LEA_RSI, anchor_va):
            if first < 7 or text.data[first - 7: first - 4] != LEA_RDI:
                continue
            unk_disp = struct.unpack_from("<i", text.data, first - 4)[0]
            unk_va = text.addr + first + unk_disp
            for second in _find_rip_refs(text, LEA_RSI, unk_va):
                for co in range(second, max(-1, second - 0x500) - 1, -1):
                    if text.data[co: co + len(FUNC_HEAD)] == FUNC_HEAD:
                        va = text.addr + co
                        if va not in candidates:
                            candidates.append(va)
                        break
    if not candidates:
        raise RuntimeError(f"未能在 {binary_path} 定位断点（可能不支持的微信版本）")
    return sorted(candidates)[0]


def find_runtime_base(pid, binary_path) -> int:
    binary_name = pathlib.Path(binary_path).name
    maps = pathlib.Path(f"/proc/{pid}/maps").read_text(encoding="utf-8")
    for line in maps.splitlines():
        p = line.split()
        if len(p) >= 6 and p[5].endswith("/" + binary_name) and "r" in p[1] and "x" in p[1]:
            return int(p[0].split("-")[0], 16)
    raise RuntimeError(f"未找到 {binary_name} 的内存基址 (PID={pid})")


# ============================================================
# GDB 抓 passphrase
# ============================================================
_GDB_SCRIPT = textwrap.dedent("""\
    set pagination off
    attach {pid}
    python
    import gdb
    class CaptureBreakpoint(gdb.Breakpoint):
        def stop(self):
            try:
                rsi = int(gdb.parse_and_eval("$rsi"))
                rdx = int(gdb.parse_and_eval("$rdx"))
                if rsi and rdx == 32:
                    raw = gdb.selected_inferior().read_memory(rsi, 32).tobytes()
                    print("WECHAT_PASSPHRASE=" + raw.hex())
                    gdb.execute("detach"); gdb.execute("quit"); return True
                if rsi:
                    size_val = int(gdb.parse_and_eval("*(unsigned long long*)($rsi+16)"))
                    if size_val == 32:
                        key_ptr = int(gdb.parse_and_eval("*(unsigned long long*)($rsi+8)"))
                        raw = gdb.selected_inferior().read_memory(key_ptr, 32).tobytes()
                        print("WECHAT_PASSPHRASE=" + raw.hex())
                        gdb.execute("detach"); gdb.execute("quit"); return True
            except Exception as e:
                print("CAPTURE_ERROR=" + str(e))
            return False
    CaptureBreakpoint("*{breakpoint_addr:#x}")
    end
    continue
    quit
""")


def check_prerequisites():
    issues = []
    if not shutil.which("gdb"):
        issues.append("未安装 GDB: sudo apt install gdb")
    try:
        scope = int(pathlib.Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip())
        if scope > 0 and os.geteuid() != 0:
            issues.append(f"ptrace_scope={scope}，需 sudo 或放开 ptrace_scope")
    except (OSError, ValueError, AttributeError):
        pass
    return issues


def _find_wechat_pid():
    for pid_str in os.listdir("/proc"):
        if not pid_str.isdigit():
            continue
        try:
            if os.readlink(f"/proc/{pid_str}/exe").endswith("/wechat"):
                return int(pid_str)
        except (OSError, PermissionError):
            continue
    raise CaptureError("微信未运行，请先启动微信")


def _find_wechat_binary(pid):
    try:
        return pathlib.Path(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        for c in (pathlib.Path("/opt/wechat/wechat"), pathlib.Path("/usr/bin/wechat")):
            if c.exists():
                return c
        raise CaptureError("未找到微信二进制")


def capture_passphrase(pid=None, timeout=120) -> str:
    pid = pid or _find_wechat_pid()
    binary_path = _find_wechat_binary(pid)
    hook_va = find_hook_offset(binary_path)
    base_addr = find_runtime_base(pid, binary_path)
    bp_addr = base_addr + hook_va
    with tempfile.TemporaryDirectory(prefix="wechat-key-") as tmp:
        sp = pathlib.Path(tmp) / "capture.gdb"
        sp.write_text(_GDB_SCRIPT.format(pid=pid, breakpoint_addr=bp_addr))
        try:
            proc = subprocess.run([shutil.which("gdb"), "-q", "--nx", "-batch", "-x", str(sp)],
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CaptureError(f"等待 {timeout}s 超时，请确认已在微信退出并重新登录。")
    combined = proc.stdout + "\n" + proc.stderr
    if "Operation not permitted" in combined:
        raise CaptureError("GDB 无法 attach，请用 sudo 或放开 ptrace_scope。")
    m = re.search(r"WECHAT_PASSPHRASE=([0-9a-fA-F]{64})", combined)
    if m:
        return m.group(1).lower()
    raise CaptureError("未捕获到 passphrase，请确认重新登录了微信。")


def load_passphrase():
    try:
        with open(PASSPHRASE_FILE) as f:
            return json.load(f).get("passphrase")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def save_passphrase(p):
    os.makedirs(os.path.dirname(PASSPHRASE_FILE), exist_ok=True)
    with open(PASSPHRASE_FILE, "w") as f:
        json.dump({"passphrase": p}, f, indent=2)
    os.chmod(PASSPHRASE_FILE, 0o600)


def _derive_keys(passphrase, db_files, salt_to_dbs):
    key_map = {}
    for i, salt_hex in enumerate(salt_to_dbs):
        enc_key = hashlib.pbkdf2_hmac("sha512", passphrase, bytes.fromhex(salt_hex), 256000, dklen=KEY_SZ)
        for _r, _p, _s, s, page1 in db_files:
            if s == salt_hex and verify_enc_key(enc_key, page1):
                key_map[salt_hex] = enc_key.hex()
                break
    return key_map


# ============================================================
# 密钥 JSON + 路径分隔符归一
# ============================================================
def _strip(keys):
    return {k: v for k, v in keys.items() if not k.startswith("_")}


def _key_path_variants(rel):
    n = rel.replace("\\", "/")
    out = []
    for c in (rel, n, n.replace("/", "\\"), n.replace("/", os.sep)):
        if c not in out:
            out.append(c)
    return out


def _get_key_info(keys, rel):
    if ".." in rel.replace("\\", "/").split("/"):
        return None
    for c in _key_path_variants(rel):
        if c in keys and not c.startswith("_"):
            return keys[c]
    return None


def _save_results(db_files, salt_to_dbs, key_map, db_dir, out_file):
    result = {}
    for rel, _p, sz, salt_hex, _pg in db_files:
        if salt_hex in key_map:
            result[rel] = {"enc_key": key_map[salt_hex], "salt": salt_hex,
                           "size_mb": round(sz / 1024 / 1024, 1)}
    if not result:
        raise RuntimeError("未能提取到任何密钥")
    result["_db_dir"] = db_dir
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    _print(f"密钥保存到: {out_file}")


def auto_detect_db_dir():
    home = pathlib.Path.home()
    candidates = [home / ".local/share/com.tencent.wechat/xwechat_files",
                  home / ".xwechat", home / ".local/share/wechat"]
    xwe = home / ".local/share/com.tencent.wechat/xwechat_files"
    if xwe.exists():
        for sub in xwe.iterdir():
            db_c = sub / "db_storage"
            if db_c.is_dir():
                candidates.insert(0, db_c)
    for pattern in (str(home / ".local/share/com.tencent.wechat/xwechat_files/*/db_storage"),
                    str(home / ".xwechat/*/db_storage")):
        for match in glob.glob(pattern):
            candidates.insert(0, pathlib.Path(match))
    for c in candidates:
        p = pathlib.Path(c)
        if p.is_dir():
            for _ in p.rglob("*.db"):
                return str(p)
    return None


def _decrypt_page(enc_key, page_data, pgno):
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        enc = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        return bytes(SQLITE_HDR + aes_cbc_decrypt(enc_key, iv, enc) + b"\x00" * RESERVE_SZ)
    enc = page_data[: PAGE_SZ - RESERVE_SZ]
    return aes_cbc_decrypt(enc_key, iv, enc) + b"\x00" * RESERVE_SZ


def _decrypt_database(db_path, out_path, enc_key):
    size = os.path.getsize(db_path)
    pages = size // PAGE_SZ + (1 if size % PAGE_SZ else 0)
    with open(db_path, "rb") as fin:
        page1 = fin.read(PAGE_SZ)
    if len(page1) < PAGE_SZ:
        return False
    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    h = hmac_mod.new(mac_key, page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ], hashlib.sha512)
    h.update(struct.pack("<I", 1))
    if h.digest() != page1[PAGE_SZ - HMAC_SZ: PAGE_SZ]:
        return False
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(db_path, "rb") as fin, open(out_path, "wb") as fout:
        for pgno in range(1, pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page += b"\x00" * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(_decrypt_page(enc_key, page, pgno))
    return True


def decrypt_all(db_dir, out_dir, keys_file):
    if not os.path.exists(keys_file):
        sys.exit(f"[ERROR] 密钥文件不存在: {keys_file}")
    keys = _strip(json.load(open(keys_file, encoding="utf-8")))
    os.makedirs(out_dir, exist_ok=True)
    db_files = []
    for root, _d, files in os.walk(db_dir):
        for fn in files:
            if fn.endswith(".db") and not fn.endswith("-wal") and not fn.endswith("-shm"):
                db_files.append((fn, os.path.join(root, fn)))
    ok = fail = 0
    for rel, path in db_files:
        info = _get_key_info(keys, rel)
        if not info or not _decrypt_database(path, os.path.join(out_dir, rel), bytes.fromhex(info["enc_key"])):
            fail += 1
        else:
            ok += 1
    _print(f"结果: {ok} 成功, {fail} 失败, 共 {len(db_files)} 个")
    return {"success": ok, "failed": fail, "total": len(db_files)}


def cmd_extract(args):
    issues = check_prerequisites()
    if issues:
        _print("[ERROR] 环境检查:", "; ".join(issues))
        sys.exit(1)
    db_dir = args.db_dir or auto_detect_db_dir()
    if not db_dir:
        sys.exit("[ERROR] 未能自动检测数据目录，请用 --db-dir 指定")
    db_files, salt_to_dbs = collect_db_files(db_dir)
    if not db_files:
        sys.exit(f"[ERROR] {db_dir} 无可解密 .db")
    out_file = args.out

    if os.path.exists(out_file):
        try:
            existing = _strip(json.load(open(out_file, encoding="utf-8")))
            if all(_get_key_info(existing, rel) and
                   verify_enc_key(bytes.fromhex(_get_key_info(existing, rel)["enc_key"]), page1)
                   for rel, _p, _s, _sa, page1 in db_files):
                _print("[+] 缓存密钥全部验证通过")
                if args.decrypt:
                    decrypt_all(db_dir, "decrypted", out_file)
                return
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    ph = load_passphrase()
    if ph:
        km = _derive_keys(bytes.fromhex(ph), db_files, salt_to_dbs)
        if km:
            _save_results(db_files, salt_to_dbs, km, db_dir, out_file)
            if args.decrypt:
                decrypt_all(db_dir, "decrypted", out_file)
            return

    _print("请在微信内：设置 → 退出登录 → 重新登录，等待最多", args.timeout, "秒...")
    ph = capture_passphrase(timeout=args.timeout)
    save_passphrase(ph)
    km = _derive_keys(bytes.fromhex(ph), db_files, salt_to_dbs)
    if not km:
        sys.exit("[ERROR] PBKDF2 派生后未验证任何密钥")
    _save_results(db_files, salt_to_dbs, km, db_dir, out_file)
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
        sys.exit("[ERROR] 未能确定数据目录，请用 --db-dir 指定")
    decrypt_all(db_dir, args.output, args.keys)


def main():
    ap = argparse.ArgumentParser(description="微信数据库密钥提取（Linux x86_64）")
    sub = ap.add_subparsers(dest="command", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--db-dir"); e.add_argument("--out", "--output", dest="out", default="all_keys.json")
    e.add_argument("--decrypt", action="store_true"); e.add_argument("--timeout", type=int, default=120)
    d = sub.add_parser("decrypt")
    d.add_argument("--db-dir"); d.add_argument("--keys", default="all_keys.json"); d.add_argument("--output", default="decrypted")
    args = ap.parse_args()
    (cmd_extract if args.command == "extract" else cmd_decrypt)(args)


if __name__ == "__main__":
    main()
