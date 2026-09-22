#!/usr/bin/env python3
"""search_fts5.py — FTS5 全文搜索模块

在加密数据库上创建 FTS5 索引并执行全文搜索，比 LIKE 快 100x+。

用法:
    # 作为模块导入
    from search_fts5 import FtsSearcher
    with FtsSearcher(db_dir, enc_key) as searcher:
        results = searcher.search("关键词", session_id="xxx@chatroom")

    # CLI
    python search_fts5.py --db-dir ... --key ... --query "关键词"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from wcdb_core import WcdbSession, find_db_files, load_keys, get_db_key_for_file, backend_name


class FtsSearcher:
    """FTS5 全文搜索器"""

    def __init__(self, db_dir: str, enc_key: str | None = None, keys_file: str | None = None):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}

    def _get_key(self, db_path: str) -> str | None:
        if self._enc_key:
            return self._enc_key
        return get_db_key_for_file(db_path, self._db_dir, self._keys)

    def _find_fts_dbs(self) -> list[str]:
        """查找所有可能包含 FTS 索引的数据库"""
        dbs = []
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if name.endswith((".db", "-wal", "-shm")):
                    continue
                path = os.path.join(root, name)
                dbs.append(path)
        return dbs

    def ensure_fts_index(self, db_path: str, enc_key: str | None = None) -> bool:
        """确保指定数据库有 FTS5 索引"""
        key = enc_key or self._get_key(db_path)
        if not key:
            return False
        try:
            with WcdbSession(db_path=db_path, enc_key=key) as db:
                # 检查是否已有 FTS 表
                existing = db.query(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'message_fts%'"
                )
                if existing:
                    return True
                # 查找消息表
                tables = db.query(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                )
                if not tables:
                    return False
                # 对每个消息表创建 FTS 索引
                for t in tables:
                    tbl = t["name"]
                    fts_name = f"message_fts_{tbl}"
                    # 检查表结构
                    cols = db.query(f"PRAGMA table_info({tbl})")
                    col_names = {c["name"] for c in cols}
                    if "message_content" not in col_names:
                        continue
                    # 创建 FTS5 虚拟表
                    db.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS {fts_name}
                        USING fts5(content, session_id, sender, ts, content={tbl}, content_rowid=rowid)
                    """)
                    # 填充数据
                    db.execute(f"""
                        INSERT OR IGNORE INTO {fts_name}(rowid, content, session_id, sender, ts)
                        SELECT rowid,
                               COALESCE(message_content, ''),
                               COALESCE(real_sender_id, ''),
                               COALESCE(sort_seq, 0),
                               COALESCE(create_time, 0)
                        FROM {tbl}
                        WHERE message_content IS NOT NULL AND message_content != ''
                    """)
                    print(f"  FTS 索引已创建: {fts_name}")
                return True
        except Exception as e:
            print(f"  [WARN] 创建 FTS 索引失败: {e}", file=sys.stderr)
            return False

    def search(
        self,
        keyword: str,
        session_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        begin_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[dict]:
        """全文搜索消息"""
        results = []
        for db_path in self._find_fts_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    # 查找 FTS 表
                    fts_tables = db.query(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'message_fts_%'"
                    )
                    if not fts_tables:
                        continue
                    for ft in fts_tables:
                        fts_name = ft["name"]
                        sql = f"""
                            SELECT snippet({fts_name}, 0, '<b>', '</b>', '...', 32) as snippet,
                                   session_id, sender, ts
                            FROM {fts_name}
                            WHERE {fts_name} MATCH ?
                            ORDER BY rank
                        """
                        params: list = [keyword]
                        if session_id:
                            sql += " AND session_id = ?"
                            params.append(session_id)
                        if begin_ts:
                            sql += " AND ts >= ?"
                            params.append(begin_ts)
                        if end_ts:
                            sql += " AND ts <= ?"
                            params.append(end_ts)
                        sql += f" LIMIT {limit} OFFSET {offset}"
                        rows = db.query(sql, tuple(params))
                        results.extend(rows)
            except Exception:
                continue
        results.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return results[:limit]


def main():
    ap = argparse.ArgumentParser(description="微信消息 FTS5 全文搜索")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex，所有库共用）")
    ap.add_argument("--keys", help="all_keys.json 路径（每个库独立密钥）")
    ap.add_argument("--query", "-q", required=True, help="搜索关键词")
    ap.add_argument("--session", help="限定会话 ID（如 xxx@chatroom）")
    ap.add_argument("--limit", type=int, default=50, help="最大返回条数")
    ap.add_argument("--offset", type=int, default=0, help="偏移量")
    ap.add_argument("--ensure-index", action="store_true", help="确保 FTS 索引存在")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    searcher = FtsSearcher(args.db_dir, args.key, args.keys)

    if args.ensure_index:
        print("正在创建 FTS 索引...")
        dbs = []
        for root, _dirs, files in os.walk(args.db_dir):
            for name in files:
                if name.endswith(".db") and not name.endswith(("-wal", "-shm")):
                    dbs.append(os.path.join(root, name))
        for db_path in dbs:
            key = searcher._get_key(db_path)
            if key:
                searcher.ensure_fts_index(db_path, key)
        print("索引创建完成")

    t0 = time.time()
    results = searcher.search(
        args.query,
        session_id=args.session,
        limit=args.limit,
        offset=args.offset,
    )
    elapsed = time.time() - t0

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"搜索「{args.query}」: {len(results)} 条结果 ({elapsed:.3f}s)\n")
        for r in results:
            ts = r.get("ts", 0)
            from datetime import datetime
            ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
            sender = r.get("sender", "?")
            snippet = r.get("snippet", "")
            print(f"[{ts_str}] {sender}: {snippet}")


if __name__ == "__main__":
    main()
