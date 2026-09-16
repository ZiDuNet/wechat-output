#!/usr/bin/env python3
"""跨平台 AES 后端抽象（纯 Python + ctypes，零第三方加密依赖）

按 ``sys.platform`` 自动选择系统密码库，向上层暴露完全一致的两个函数签名：

- ``aes_ecb_decrypt(key: bytes, data: bytes) -> bytes``
    AES-128-ECB 解密、无 padding（微信图片缩略图密钥用，见 media_common）。
- ``aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes``
    AES-256-CBC 解密、无 padding（SQLCipher4 整库逐页解密用）。

三后端（全部走操作系统自带系统库，不引入 pycryptodome/cryptography）：

| 平台 | 系统库 | 实现 |
|------|--------|------|
| win32 | Windows CNG ``bcrypt.dll`` | BCryptOpenAlgorithmProvider + BCryptDecrypt（与旧版 media_common / wcdb_key_tool_windows 逐字节一致） |
| darwin | macOS ``libSystem`` CommonCrypto ``CCCrypt`` | 上游 TANGandXue/wcdb-key-tool 移植 |
| linux | ``libcrypto.so`` OpenSSL EVP | 上游 TANGandXue/wcdb-key-tool 移植 |

验证级别：
- win32 后端：【已验证】Windows 本机真机回归（见 README/SKILL.md 跨平台章节）。
- darwin / linux 后端：【代码级验证，未真机】仅在 Windows 上做语法/import/逻辑走查，
  未在 macOS / Linux 真机跑过；逻辑直接移植自上游已真机验证过的脚本。

移植自上游 TANGandXue/wcdb-key-tool（MIT）的 macOS ``wcdb_key_tool_macos.py`` 与
Linux ``wcdb_key_tool.py``。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys

# 群名/密钥可能含非常规字符；stdout 重定向到管道时替换而非崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# ---------------------------------------------------------------- 后端选择日志
BACKEND = sys.platform
print(f"[aes_backend] 选择密码后端: {BACKEND}", flush=True)


# ============================================================
# Windows 后端：CNG bcrypt.dll（与旧实现逐字节一致，保持现状不回归）
# ============================================================
if sys.platform == "win32":
    import ctypes.wintypes as _wt

    _bcrypt = ctypes.WinDLL("bcrypt")
    _bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(_wt.HANDLE), ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    _bcrypt.BCryptSetProperty.argtypes = [
        _wt.HANDLE, ctypes.c_wchar_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong]
    _bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        _wt.HANDLE, ctypes.POINTER(_wt.HANDLE), ctypes.c_char_p, ctypes.c_ulong,
        ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong]
    _bcrypt.BCryptDecrypt.argtypes = [
        _wt.HANDLE, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong]
    _bcrypt.BCryptDestroyKey.argtypes = [_wt.HANDLE]
    _bcrypt.BCryptCloseAlgorithmProvider.argtypes = [_wt.HANDLE, ctypes.c_ulong]

    def _bcrypt_decrypt(key: bytes, chaining_mode: str, data: bytes,
                        iv: bytes | None = None) -> bytes:
        """CNG 通用解密：chaining_mode 取 "ChainingModeECB" 或 "ChainingModeCBC"。"""
        h_alg = _wt.HANDLE()
        status = _bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0)
        if status != 0:
            raise RuntimeError(f"BCryptOpenAlgorithmProvider failed: {status:#x}")
        try:
            mode = (chaining_mode + "\x00").encode("utf-16-le")
            status = _bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, len(mode), 0)
            if status != 0:
                raise RuntimeError(f"BCryptSetProperty failed: {status:#x}")
            h_key = _wt.HANDLE()
            status = _bcrypt.BCryptGenerateSymmetricKey(
                h_alg, ctypes.byref(h_key), None, 0, key, len(key), 0)
            if status != 0:
                raise RuntimeError(f"BCryptGenerateSymmetricKey failed: {status:#x}")
            try:
                iv_buf = ctypes.create_string_buffer(iv, len(iv)) if iv else None
                out_buf = ctypes.create_string_buffer(len(data))
                result_len = ctypes.c_ulong(0)
                status = _bcrypt.BCryptDecrypt(
                    h_key, data, len(data), None,
                    iv_buf, len(iv) if iv else 0,
                    out_buf, len(out_buf), ctypes.byref(result_len), 0)
                if status != 0:
                    raise RuntimeError(f"BCryptDecrypt failed: {status:#x}")
                return out_buf.raw[: result_len.value]
            finally:
                _bcrypt.BCryptDestroyKey(h_key)
        finally:
            _bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)

    def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
        """AES-ECB 解密（无 padding），CNG bcrypt.dll。"""
        return _bcrypt_decrypt(key, "ChainingModeECB", data)

    def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        """AES-256-CBC 解密（无 padding），CNG bcrypt.dll。"""
        return _bcrypt_decrypt(key, "ChainingModeCBC", data, iv)


# ============================================================
# macOS 后端：CommonCrypto CCCrypt（系统自带，零第三方依赖）
# ============================================================
elif sys.platform == "darwin":
    # 【代码级验证，未真机】移植自上游 wcdb_key_tool_macos.py
    _libSystem = ctypes.CDLL(ctypes.util.find_library("System"))
    _libSystem.CCCrypt.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.c_char_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _libSystem.CCCrypt.restype = ctypes.c_int32

    _kCCDecrypt = 1
    _kCCAlgorithmAES = 0
    _kCCOptionECBMode = 2
    _ZERO_IV = b"\x00" * 16

    def _cccrypt_decrypt(key: bytes, iv: bytes, data: bytes, ecb: bool) -> bytes:
        options = _kCCOptionECBMode if ecb else 0  # options=0 -> 无 padding，SQLCipher 自管
        out_buf = ctypes.create_string_buffer(len(data) + 32)
        out_len = ctypes.c_size_t(0)
        status = _libSystem.CCCrypt(
            _kCCDecrypt, _kCCAlgorithmAES, options,
            key, len(key),
            _ZERO_IV if ecb else iv,
            data, len(data),
            out_buf, len(out_buf),
            ctypes.byref(out_len),
        )
        if status != 0:
            raise RuntimeError(f"CCCrypt 解密失败: status={status}")
        return out_buf.raw[: out_len.value]

    def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
        """AES-ECB 解密（无 padding），CommonCrypto。"""
        return _cccrypt_decrypt(key, b"", data, ecb=True)

    def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        """AES-256-CBC 解密（无 padding），CommonCrypto。"""
        return _cccrypt_decrypt(key, iv, data, ecb=False)


# ============================================================
# Linux 后端：OpenSSL EVP（libcrypto.so，零第三方依赖）
# ============================================================
elif sys.platform.startswith("linux"):
    # 【代码级验证，未真机】移植自上游 wcdb_key_tool.py
    _ssl: "ctypes.CDLL | None" = None

    def _load_openssl() -> ctypes.CDLL:
        for name in ("crypto", "ssl"):
            lib_name = ctypes.util.find_library(name)
            if lib_name:
                try:
                    return ctypes.CDLL(lib_name)
                except OSError:
                    pass
        for path in ("/usr/lib/x86_64-linux-gnu/libcrypto.so.3",
                     "/usr/lib/x86_64-linux-gnu/libcrypto.so.1.1",
                     "/lib/x86_64-linux-gnu/libcrypto.so.3"):
            if os.path.exists(path):
                return ctypes.CDLL(path)
        raise RuntimeError("未找到 libcrypto/libssl，请安装: sudo apt install libssl-dev")

    def _get_ssl() -> "ctypes.CDLL":
        global _ssl
        if _ssl is None:
            _ssl = _load_openssl()
        return _ssl

    def _evp_decrypt(key: bytes, iv: bytes, data: bytes, ecb: bool) -> bytes:
        ssl = _get_ssl()
        ctx = ssl.EVP_CIPHER_CTX_new()
        if not ctx:
            raise RuntimeError("EVP_CIPHER_CTX_new 失败")
        try:
            cipher = ssl.EVP_aes_128_ecb() if ecb else ssl.EVP_aes_256_cbc()
            if not cipher:
                raise RuntimeError(f"{'EVP_aes_128_ecb' if ecb else 'EVP_aes_256_cbc'} 失败")
            key_arr = (ctypes.c_ubyte * len(key))(*key)
            # ECB 模式不需要 IV（传 None）；CBC 需要 16 字节 IV
            iv_arr = (ctypes.c_ubyte * len(iv))(*iv) if (not ecb and iv) else None
            ret = ssl.EVP_DecryptInit_ex(ctx, cipher, None, key_arr, iv_arr)
            if ret != 1:
                raise RuntimeError("EVP_DecryptInit_ex 失败")
            ssl.EVP_CIPHER_CTX_set_padding(ctx, 0)  # 关闭 padding（SQLCipher/图片区自管）
            out_buf = ctypes.create_string_buffer(len(data) + 32)
            out_len = ctypes.c_int(0)
            ret = ssl.EVP_DecryptUpdate(ctx, out_buf, ctypes.byref(out_len),
                                        ctypes.c_char_p(data), len(data))
            if ret != 1:
                raise RuntimeError("EVP_DecryptUpdate 失败")
            final_buf = ctypes.create_string_buffer(32)
            final_len = ctypes.c_int(0)
            ssl.EVP_DecryptFinal_ex(ctx, final_buf, ctypes.byref(final_len))
            return out_buf.raw[: out_len.value + final_len.value]
        finally:
            ssl.EVP_CIPHER_CTX_free(ctx)

    def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
        """AES-ECB 解密（无 padding），OpenSSL EVP。"""
        return _evp_decrypt(key, b"", data, ecb=True)

    def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        """AES-256-CBC 解密（无 padding），OpenSSL EVP。"""
        return _evp_decrypt(key, iv, data, ecb=False)


else:
    raise RuntimeError(f"aes_backend: 不支持的平台 {sys.platform!r}")
