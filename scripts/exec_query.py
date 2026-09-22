#!/usr/bin/env python3
"""exec_query.py — 通用 SQL 执行器

在加密数据库上执行任意 SQL 查询。

用法:
    from exec_query import SqlExecutor
    with SqlExecutor(db_dir, enc_key) as executor:
        result = executor.query("SELECT * FROM contact LIMIT 10")
        executor.execute("UPDATE contact SET remark='test' WHERE username='xxx'")

    # CLI
    python exec_query.py --db-dir ... --key ... "SELECT * FROM contact LIMIT 10"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from wcdb_core import WcdbSession, load_keys, get_db_key_for_file


class SqlExecutor:
    """通用 SQL 执行器"""

    def __init__(self, db_dir: str, enc_key: str | None = None, keys_file: str | None = None):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}

    def _get_key(self, db_path: str) -> str | None:
        if self._enc_key:
            return self._enc_key
        return get_db_key_for_file(db_path, self._db_dir, self._keys)

    def _find_db(self, name: str) -> str | None:
        """按名称查找数据库"""
        for root, _dirs, files in os.walk(self._db_dir):
            for fname in files:
                if fname == name and not fname.endswith(("-wal", "-shm")):
                    return os.path.join(root, fname)
                if fname.startswith(name) and fname.endswith(".db") and not fname.endswith(("-wal", "-shm")):
                    return os.path.join(root, fname)
        return None

    def query(self, sql: str, db_name: str | None = None, params: tuple = ()) -> list[dict]:
        """执行查询"""
        db_path = self._find_db(db_name) if db_name else self._find_any_db()
        if not db_path:
            raise FileNotFoundError(f"找不到数据库: {db_name or '任何数据库'}")

        key = self._get_key(db_path)
        if not key:
            raise RuntimeError(f"找不到密钥: {db_path}")

        with WcdbSession(db_path=db_path, enc_key=key) as db:
            return db.query(sql, params)

    def execute(self, sql: str, db_name: str | None = None, params: tuple = ()) -> int:
        """执行写操作"""
        db_path = self._find_db(db_name) if db_name else self._find_any_db()
        if not db_path:
            raise FileNotFoundError(f"找不到数据库: {db_name or '任何数据库'}")

        key = self._get_key(db_path)
        if not key:
            raise RuntimeError(f"找不到密钥: {db_path}")

        with WcdbSession(db_path=db_path, enc_key=key) as db:
            return db.execute(sql, params)

    def _find_any_db(self) -> str | None:
        """查找任意一个数据库"""
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if name.endswith(".db") and not name.endswith(("-wal", "-shm")):
                    return os.path.join(root, name)
        return None

    def list_tables(self, db_name: str | None = None) -> list[str]:
        """列出所有表"""
        rows = self.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name", db_name)
        return [r["name"] for r in rows]

    def table_info(self, table_name: str, db_name: str | None = None) -> list[dict]:
        """获取表结构"""
        return self.query(f"PRAGMA table_info({table_name})", db_name)

    def table_count(self, table_name: str, db_name: str | None = None) -> int:
        """获取表行数"""
        rows = self.query(f"SELECT COUNT(*) as cnt FROM {table_name}", db_name)
        return rows[0]["cnt"] if rows else 0

    def search_tables(self, keyword: str, db_name: str | None = None) -> list[str]:
        """搜索表名"""
        tables = self.list_tables(db_name)
        return [t for t in tables if keyword.lower() in t.lower()]


def main():
    ap = argparse.ArgumentParser(description="通用 SQL 执行器")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex）")
    ap.add_argument("--keys", help="all_keys.json 路径")
    ap.add_argument("--db", help="指定数据库名称")
    sub = ap.add_subparsers(dest="cmd")

    # query 子命令
    q_p = sub.add_parser("query", help="执行查询")
    q_p.add_argument("sql", help="SQL 语句")
    q_p.add_argument("--json", action="store_true", help="JSON 输出")

    # tables 子命令
    t_p = sub.add_parser("tables", help="列出表")
    t_p.add_argument("--search", help="搜索关键词")

    # info 子命令
    i_p = sub.add_parser("info", help="表结构")
    i_p.add_argument("table_name", help="表名")

    # count 子命令
    c_p = sub.add_parser("count", help="表行数")
    c_p.add_argument("table_name", help="表名")

    # search 子命令
    s_p = sub.add_parser("search", help="搜索表")
    s_p.add_argument("keyword", help="关键词")

    args = ap.parse_args()

    executor = SqlExecutor(args.db_dir, args.key, getattr(args, 'keys', None))

    if args.cmd == "query":
        rows = executor.query(args.sql, args.db)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            if not rows:
                print("无结果")
            else:
                # 表格输出
                headers = rows[0].keys()
                widths = {h: max(len(str(h)), max(len(str(r.get(h, ""))) for r in rows)) for h in headers}
                header_line = " | ".join(f"{h:<{widths[h]}}" for h in headers)
                print(header_line)
                print("-+-".join("-" * widths[h] for h in headers))
                for r in rows:
                    print(" | ".join(f"{str(r.get(h, '')):<{widths[h]}}" for h in headers))

    elif args.cmd == "tables":
        tables = executor.list_tables(args.db)
        if args.search:
            tables = [t for t in tables if args.search.lower() in t.lower()]
        for t in tables:
            print(t)

    elif args.cmd == "info":
        info = executor.table_info(args.table_name, args.db)
        print(f"表结构: {args.table_name}")
        for col in info:
            print(f"  {col['name']}: {col['type']} {'NOT NULL' if col['notnull'] else ''} "
                  f"{'PRIMARY KEY' if col['pk'] else ''}")

    elif args.cmd == "count":
        cnt = executor.table_count(args.table_name, args.db)
        print(f"{args.table_name}: {cnt} 行")

    elif args.cmd == "search":
        tables = executor.search_tables(args.keyword, args.db)
        for t in tables:
            print(t)

    else:
        ap.print_help()


if __name__ == "__main__":
    main()
