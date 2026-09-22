#!/usr/bin/env python3
"""cursor_fetch.py — 游标分批拉取模块

大群消息分批拉取，避免一次性 fetchall() 导致 OOM。

用法:
    from cursor_fetch import MessageCursor
    with MessageCursor(db_dir, enc_key, session_id="xxx@chatroom") as cursor:
        for batch in cursor.batches(batch_size=500):
            for msg in batch:
                process(msg)

    # CLI
    python cursor_fetch.py --db-dir ... --key ... --session "群名" --batch 500
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from typing import Iterator

from wcdb_core import WcdbSession, find_db_files, find_session_table, load_keys, get_db_key_for_file


class MessageCursor:
    """消息游标 —— 分批拉取指定会话的消息"""

    def __init__(
        self,
        db_dir: str,
        enc_key: str | None = None,
        keys_file: str | None = None,
        session_id: str | None = None,
        ascending: bool = True,
        begin_ts: int | None = None,
        end_ts: int | None = None,
    ):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}
        self._session_id = session_id
        self._ascending = ascending
        self._begin_ts = begin_ts
        self._end_ts = end_ts
        self._conn = None
        self._db_files: list[str] = []

    def _get_key(self, db_path: str) -> str | None:
        if self._enc_key:
            return self._enc_key
        return get_db_key_for_file(db_path, self._db_dir, self._keys)

    def _find_message_dbs(self) -> list[str]:
        """查找所有消息库"""
        dbs = []
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if name.startswith("message") and name.endswith(".db") and not name.endswith(("-wal", "-shm")):
                    dbs.append(os.path.join(root, name))
        return sorted(dbs)

    def _find_session_table(self, db: WcdbSession) -> str | None:
        """在数据库中查找会话对应的消息表（Msg_<md5(会话名)>，与主链 export_group_md 一致）"""
        if not self._session_id:
            # 没有指定会话，返回第一个消息表
            tables = db.query(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            )
            return tables[0]["name"] if tables else None

        return find_session_table(db, self._session_id)

    def _build_sql(self, table: str) -> tuple[str, list]:
        """构建分页查询 SQL"""
        conditions = []
        params: list = []

        # 会话表本身即该会话的全部消息，不再按 real_sender_id 过滤
        # （群聊消息的 real_sender_id 是成员 rid，不是会话 rid，原写法对群聊恒为 0 条）

        if self._begin_ts:
            conditions.append("create_time >= ?")
            params.append(self._begin_ts)
        if self._end_ts:
            conditions.append("create_time <= ?")
            params.append(self._end_ts)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        order = "ASC" if self._ascending else "DESC"

        sql = f"""
            SELECT rowid, server_id, local_type, sort_seq, real_sender_id,
                   create_time, status, message_content, source
            FROM {table}
            {where}
            ORDER BY create_time {order}, sort_seq {order}
            LIMIT ? OFFSET ?
        """
        return sql, params

    def batches(self, batch_size: int = 500) -> Iterator[list[dict]]:
        """分批迭代消息"""
        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    table = self._find_session_table(db)
                    if not table:
                        continue
                    sql, params = self._build_sql(table)
                    offset = 0
                    while True:
                        batch_params = tuple(params) + (batch_size, offset)
                        rows = db.query(sql, batch_params)
                        if not rows:
                            break
                        yield rows
                        offset += batch_size
                        if len(rows) < batch_size:
                            break
            except Exception as e:
                print(f"  [WARN] 读取 {db_path} 失败: {e}", file=sys.stderr)
                continue

    def count(self) -> int:
        """统计总消息数"""
        total = 0
        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    table = self._find_session_table(db)
                    if not table:
                        continue
                    conditions = []
                    params: list = []
                    if self._begin_ts:
                        conditions.append("create_time >= ?")
                        params.append(self._begin_ts)
                    if self._end_ts:
                        conditions.append("create_time <= ?")
                        params.append(self._end_ts)
                    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
                    result = db.query(f"SELECT COUNT(*) as cnt FROM {table} {where}", tuple(params))
                    if result:
                        total += result[0]["cnt"]
            except Exception:
                continue
        return total

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _content_to_text(content) -> str:
    """消息内容转可读文本（bytes 可能是 zstd 压缩或原始字节，JSON 输出需先转 str）"""
    if content is None:
        return ""
    if isinstance(content, bytes):
        if content[:4] == b"\x28\xb5\x2f\xfd":  # zstd magic
            return "[zstd 压缩，需 zstandard 解压]"
        return content.decode("utf-8", errors="replace")
    return str(content)


def _json_safe(row: dict) -> dict:
    """把记录中所有 bytes 值转 str（message_content / source 等列在微信里可能是原始字节）"""
    out = {}
    for k, v in row.items():
        out[k] = _content_to_text(v) if isinstance(v, bytes) else v
    return out


def main():
    ap = argparse.ArgumentParser(description="微信消息分批拉取")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex）")
    ap.add_argument("--keys", help="all_keys.json 路径")
    ap.add_argument("--session", "-s", help="会话 ID（如 xxx@chatroom）")
    ap.add_argument("--batch", type=int, default=500, help="每批条数")
    ap.add_argument("--count", action="store_true", help="只统计数量")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    cursor = MessageCursor(
        args.db_dir, args.key, args.keys,
        session_id=args.session,
    )

    if args.count:
        total = cursor.count()
        print(f"总消息数: {total}")
        return

    total = 0
    t0 = time.time()
    for batch in cursor.batches(args.batch):
        total += len(batch)
        if args.json:
            for msg in batch:
                print(json.dumps(_json_safe(msg), ensure_ascii=False))
        else:
            for msg in batch:
                ts = msg.get("create_time", 0)
                ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
                sender = msg.get("real_sender_id", "?")
                content = _content_to_text(msg.get("message_content"))[:80]
                print(f"[{ts_str}] {sender}: {content}")
        if not args.json:
            print(f"  ... 已拉取 {total} 条", file=sys.stderr)

    elapsed = time.time() - t0
    print(f"\n完成: {total} 条消息 ({elapsed:.2f}s)", file=sys.stderr)


if __name__ == "__main__":
    main()
