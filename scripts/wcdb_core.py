#!/usr/bin/env python3
"""wcdb_core.py — 微信加密数据库直连核心模块

提供统一的数据库访问层，支持三种后端（按优先级自动降级）：
  1. pysqlcipher3  — Python 原生 SQLCipher 绑定（首选，零磁盘写入）
  2. sqlcipher CLI — 命令行工具（回退，通过 subprocess 调用）
  3. 解密到磁盘    — 现有方案的降级备选（需先跑 decrypt）

用法:
    from wcdb_core import WcdbSession

    # 方式1：直连加密库（推荐）
    with WcdbSession("/path/to/db_storage", enc_key="64hex...") as db:
        rows = db.query("SELECT * FROM contact LIMIT 10")

    # 方式2：读已解密的库（降级）
    with WcdbSession(dec_dir="/path/to/decrypted") as db:
        rows = db.query("SELECT * FROM contact LIMIT 10")
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

# ============================================================
# 后端检测
# ============================================================

_HAS_PYSQLCIPHER3 = False
try:
    from pysqlcipher3 import dbapi2 as sqlcipher
    _HAS_PYSQLCIPHER3 = True
except ImportError:
    pass

_HAS_SQLCIPHER_CLI = False
try:
    r = subprocess.run(["sqlcipher", "--version"], capture_output=True, timeout=5)
    if r.returncode == 0:
        _HAS_SQLCIPHER_CLI = True
except Exception:
    pass


def backend_name() -> str:
    """返回当前使用的后端名称"""
    if _HAS_PYSQLCIPHER3:
        return "pysqlcipher3"
    if _HAS_SQLCIPHER_CLI:
        return "sqlcipher_cli"
    return "decrypt_fallback"


# ============================================================
# SQLCipher 参数（微信 4.x 用 SQLCipher 4）
# ============================================================

SQLCIPHER_PARAMS = {
    "cipher_compatibility": 4,
    "cipher_page_size": 4096,
    "cipher_kdf_iter": 256000,
    "cipher_hmac_algorithm": "HMAC_SHA512",
    "cipher_kdf_algorithm": "PBKDF2_HMAC_SHA512",
    "cipher_use_hmac": 1,
}


# ============================================================
# WcdbSession — 统一数据库会话
# ============================================================

class WcdbSession:
    """微信加密数据库统一访问层

    支持三种打开方式：
      - enc_key + db_dir:  直连加密库（首选）
      - dec_dir:           读已解密的明文库（降级）
      - db_path:           直接指定单个 .db 文件
    """

    def __init__(
        self,
        db_dir: str | None = None,
        dec_dir: str | None = None,
        db_path: str | None = None,
        enc_key: str | None = None,
        wxid: str | None = None,
        readonly: bool = True,
    ):
        """readonly=True 以只读方式打开（默认，避免干扰正在运行的微信）；
        readonly=False 允许写入（仅 FTS 建索引 / 显式 execute 等确有写需求时使用）"""
        self._conn = None
        self._backend = backend_name()
        self._db_dir = db_dir
        self._dec_dir = dec_dir
        self._db_path = db_path
        self._enc_key = enc_key
        self._wxid = wxid
        self._readonly = readonly

        # 验证参数
        if not any([db_dir, dec_dir, db_path]):
            raise ValueError("需要指定 db_dir、dec_dir 或 db_path 之一")
        if db_dir and not enc_key:
            raise ValueError("直连加密库需要提供 enc_key")
        if dec_dir and enc_key:
            # 有 key 优先直连，忽略 dec_dir
            self._db_dir = db_dir or dec_dir
            self._dec_dir = None

    def connect(self, db_file: str | None = None):
        """连接到数据库"""
        target = db_file or self._db_path
        if not target and self._dec_dir:
            raise ValueError("dec_dir 模式需要通过 connect(db_file) 指定具体文件")

        if self._enc_key:
            return self._connect_encrypted(target)
        elif self._dec_dir:
            return self._connect_plaintext(target)
        else:
            raise ValueError("无法确定连接方式")

    def _connect_encrypted(self, db_path: str):
        """直连加密库"""
        if _HAS_PYSQLCIPHER3:
            return self._connect_pysqlcipher3(db_path)
        elif _HAS_SQLCIPHER_CLI:
            return self._connect_cli(db_path)
        else:
            raise RuntimeError(
                "无法直连加密库：pysqlcipher3 和 sqlcipher 均不可用。\n"
                "安装 pysqlcipher3:  pip install pysqlcipher3\n"
                "或安装 sqlcipher:   apt install sqlcipher / brew install sqlcipher"
            )

    def _connect_pysqlcipher3(self, db_path: str):
        """pysqlcipher3 直连：默认只读（避免干扰正在运行的微信），可显式开写"""
        if self._readonly:
            uri = "file:" + os.path.abspath(db_path).replace(os.sep, "/") + "?mode=ro"
            self._conn = sqlcipher.connect(uri, uri=True)
        else:
            self._conn = sqlcipher.connect(db_path)
        self._conn.row_factory = sqlcipher.Row
        key_hex = self._enc_key if self._enc_key.startswith("x'") else f"x'{self._enc_key}'"
        for param, val in SQLCIPHER_PARAMS.items():
            self._conn.execute(f"PRAGMA {param} = {val}")
        self._conn.execute(f'PRAGMA key = "{key_hex}"')
        # 验证密钥
        try:
            self._conn.execute("SELECT count(*) FROM sqlite_master")
        except Exception as e:
            self._conn.close()
            self._conn = None
            raise RuntimeError(f"密钥验证失败: {e}")
        return self._conn

    def _connect_cli(self, db_path: str):
        """sqlcipher CLI 模式 —— 通过临时 SQL 文件执行"""
        self._cli_db_path = db_path
        return self

    def _connect_plaintext(self, db_path: str):
        """打开已解密的明文库"""
        import sqlite3
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        return self._conn

    def _using_cli(self) -> bool:
        return bool(getattr(self, "_cli_db_path", None))

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """执行查询，返回字典列表

        CLI 后端无参数绑定，仅支持 str/int/float 参数内联，含引号或 SQL 字面量内 `?` 的查询请直接拼 SQL。
        """
        if self._using_cli():
            return self._query_cli(sql, params)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def execute(self, sql: str, params: tuple = ()) -> int:
        """执行写操作，返回影响行数（readonly 模式禁止写）"""
        if self._using_cli():
            return self._execute_cli(sql, params)
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur.rowcount

    def executescript(self, script: str):
        """执行多条 SQL（readonly 模式禁止写）"""
        if self._using_cli():
            self._execute_cli(script, ())
            return
        self._conn.executescript(script)

    def _query_cli(self, sql: str, params: tuple) -> list[dict]:
        """通过 sqlcipher CLI 查询"""
        formatted_sql = sql
        for i, p in enumerate(params):
            if isinstance(p, str):
                formatted_sql = formatted_sql.replace("?", f"'{p}'", 1)
            elif isinstance(p, (int, float)):
                formatted_sql = formatted_sql.replace("?", str(p), 1)
        # 构建完整的 SQL 脚本
        script = f"PRAGMA key = '{self._enc_key}';\n"
        for k, v in SQLCIPHER_PARAMS.items():
            script += f"PRAGMA {k} = {v};\n"
        script += f".mode json\n{formatted_sql};\n"
        r = subprocess.run(
            ["sqlcipher", self._cli_db_path],
            input=script,
            capture_output=True,
            text=True,
            timeout=30
        )
        if r.returncode != 0:
            raise RuntimeError(f"sqlcipher 查询失败: {r.stderr}")
        if not r.stdout.strip():
            return []
        try:
            # sqlcipher 可能在 JSON 前输出 "ok"，需要跳过
            output = r.stdout.strip()
            lines = output.split('\n')
            # 找到 JSON 开始的位置（第一个 [ 或 {）
            json_start = 0
            for i, line in enumerate(lines):
                if line.strip().startswith('[') or line.strip().startswith('{'):
                    json_start = i
                    break
            json_str = '\n'.join(lines[json_start:])
            data = json.loads(json_str)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            # 无结果集时 CLI 只输出 "ok"（无 [ 或 {）：是空结果，不是 [{"result":"ok"}]
            if not any(ch in r.stdout for ch in "[{"):
                return []
            return [{"result": r.stdout.strip()}]

    def _execute_cli(self, sql: str, params: tuple) -> int:
        """通过 sqlcipher CLI 执行写操作（无参数绑定，仅支持 str/int/float 内联）"""
        if self._readonly:
            raise RuntimeError("WcdbSession(readonly=True) 禁止写操作，请传 readonly=False")
        formatted_sql = sql
        for i, p in enumerate(params):
            if isinstance(p, str):
                formatted_sql = formatted_sql.replace("?", f"'{p}'", 1)
            else:
                formatted_sql = formatted_sql.replace("?", str(p), 1)
        script = f"PRAGMA key = '{self._enc_key}';\n"
        for k, v in SQLCIPHER_PARAMS.items():
            script += f"PRAGMA {k} = {v};\n"
        script += f"{formatted_sql};\n"
        r = subprocess.run(
            ["sqlcipher", self._cli_db_path],
            input=script,
            capture_output=True,
            text=True,
            timeout=30
        )
        if r.returncode != 0:
            raise RuntimeError(f"sqlcipher 执行失败: {r.stderr}")
        return 0

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()


# ============================================================
# 工具函数
# ============================================================

def name2id_col(db) -> str | None:
    """探测 name2id 的用户名列（user_name / username，微信不同版本列名不同）"""
    for c in db.query("PRAGMA table_info(name2id)"):
        if c["name"] in ("user_name", "username"):
            return c["name"]
    return None


def find_session_table(db, session_id: str) -> str | None:
    """按会话 username 定位消息表（与主链 export_group_md 一致：Msg_<md5(username)>）。

    注意 session_id 是会话 username/wxid，不是显示群名；
    表名不是 md5 命名时（测试库/旧库），遍历 Msg 表用 name2id 反查兜底。
    """
    import hashlib
    tbl = f"Msg_{hashlib.md5(session_id.encode('utf-8')).hexdigest()}"
    if db.query("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (tbl,)):
        return tbl
    col = name2id_col(db)
    if col:
        session_map = {}
        for r in db.query(f"SELECT {col} AS u FROM name2id"):
            u = r.get("u")
            if u:
                session_map[hashlib.md5(u.encode("utf-8")).hexdigest()] = u
        for t in db.query("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
            if session_map.get(t["name"][4:].lower()) == session_id:
                return t["name"]
    return None


def find_db_files(db_dir: str) -> dict[str, list[str]]:
    """扫描 db_storage 目录，返回 {类别: [路径列表]}"""
    categories = {
        "message": [], "contact": [], "session": [],
        "media": [], "publicmsg": [], "sns": [],
        "favorite": [], "other": [],
    }
    for root, _dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db") or name.endswith(("-wal", "-shm")):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, db_dir)
            lower = rel.lower()
            if "message" in lower:
                categories["message"].append(path)
            elif "contact" in lower:
                categories["contact"].append(path)
            elif "session" in lower:
                categories["session"].append(path)
            elif "media" in lower:
                categories["media"].append(path)
            elif "publicmsg" in lower or "biz" in lower:
                categories["publicmsg"].append(path)
            elif "sns" in lower:
                categories["sns"].append(path)
            elif "fav" in lower:
                categories["favorite"].append(path)
            else:
                categories["other"].append(path)
    return categories


def load_keys(keys_file: str) -> dict[str, str]:
    """加载 all_keys.json，返回 {db_rel_path: enc_key_hex}"""
    with open(keys_file, encoding="utf-8") as f:
        data = json.load(f)
    result = {}
    for rel, info in data.items():
        if isinstance(info, dict) and "enc_key" in info:
            result[rel] = info["enc_key"]
        elif isinstance(info, str):
            result[rel] = info
    return result


def get_db_key_for_file(db_path: str, db_dir: str, keys: dict[str, str]) -> str | None:
    """根据数据库路径获取对应的密钥"""
    rel = os.path.relpath(db_path, db_dir)
    # 尝试直接匹配
    if rel in keys:
        return keys[rel]
    # 尝试正斜杠
    rel_fwd = rel.replace("\\", "/")
    if rel_fwd in keys:
        return keys[rel_fwd]
    # 模糊匹配（只按文件名）
    basename = os.path.basename(db_path)
    for k, v in keys.items():
        if os.path.basename(k) == basename:
            return v
    return None


# ============================================================
# CLI 入口
# ============================================================

def main():
    import argparse
    ap = argparse.ArgumentParser(description="微信数据库直连工具")
    sub = ap.add_subparsers(dest="cmd")

    # info 子命令
    info_p = sub.add_parser("info", help="查看数据库信息")
    info_p.add_argument("db_path", help="数据库路径")
    info_p.add_argument("--key", help="密钥（64位hex）")

    # query 子命令
    query_p = sub.add_parser("query", help="执行 SQL 查询")
    query_p.add_argument("db_path", help="数据库路径")
    query_p.add_argument("sql", help="SQL 语句")
    query_p.add_argument("--key", help="密钥（64位hex）")

    # scan 子命令
    scan_p = sub.add_parser("scan", help="扫描 db_storage")
    scan_p.add_argument("db_dir", help="db_storage 目录")
    scan_p.add_argument("--keys", help="all_keys.json 路径")

    args = ap.parse_args()

    if args.cmd == "info":
        key = args.key
        if key:
            with WcdbSession(db_path=args.db_path, enc_key=key) as db:
                tables = db.query("SELECT name FROM sqlite_master WHERE type='table'")
                print(f"后端: {backend_name()}")
                print(f"表数量: {len(tables)}")
                for t in tables:
                    print(f"  - {t['name']}")
        else:
            import sqlite3
            conn = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            print(f"后端: sqlite3 (明文)")
            print(f"表数量: {len(tables)}")
            for t in tables:
                print(f"  - {t[0]}")
            conn.close()

    elif args.cmd == "query":
        if args.key:
            with WcdbSession(db_path=args.db_path, enc_key=args.key) as db:
                rows = db.query(args.sql)
                print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            import sqlite3
            conn = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(args.sql).fetchall()]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            conn.close()

    elif args.cmd == "scan":
        cats = find_db_files(args.db_dir)
        for cat, paths in cats.items():
            if paths:
                print(f"\n{cat} ({len(paths)}):")
                for p in sorted(paths):
                    print(f"  {os.path.relpath(p, args.db_dir)}")
        if args.keys:
            keys = load_keys(args.keys)
            print(f"\n密钥: {len(keys)} 个")
            for rel in sorted(keys):
                print(f"  {rel}: {keys[rel][:8]}...")

    else:
        ap.print_help()


if __name__ == "__main__":
    main()
