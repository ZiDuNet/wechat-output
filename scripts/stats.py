#!/usr/bin/env python3
"""stats.py — 统计分析模块（可选功能）

提供消息统计、年度报告、群组统计等功能。

用法:
    from stats import StatsAnalyzer
    with StatsAnalyzer(db_dir, enc_key) as sa:
        overview = sa.get_overview()
        session_stats = sa.get_session_stats("xxx@chatroom")

    # CLI
    python stats.py --db-dir ... --key ... overview
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

from wcdb_core import WcdbSession, find_session_table, load_keys, get_db_key_for_file, name2id_col


class StatsAnalyzer:
    """统计分析器"""

    def __init__(self, db_dir: str, enc_key: str | None = None, keys_file: str | None = None):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}

    def _get_key(self, db_path: str) -> str | None:
        if self._enc_key:
            return self._enc_key
        return get_db_key_for_file(db_path, self._db_dir, self._keys)

    def _find_contact_db(self) -> str | None:
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if name == "contact.db" and not name.endswith(("-wal", "-shm")):
                    return os.path.join(root, name)
        return None

    def _find_message_dbs(self) -> list[str]:
        dbs = []
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if name.startswith("message") and name.endswith(".db") and not name.endswith(("-wal", "-shm")):
                    dbs.append(os.path.join(root, name))
        return sorted(dbs)

    @staticmethod
    def _name2id_col(db) -> str | None:
        """name2id 列名兼容（user_name / username）"""
        return name2id_col(db)

    def _find_session_table(self, db, session_id: str) -> str | None:
        return find_session_table(db, session_id)

    def get_overview(self) -> dict:
        """获取总览统计"""
        result = {
            "contacts": {"groups": 0, "officials": 0, "privates": 0},
            "messages": {"total": 0, "by_type": {}},
            "sessions": {"total": 0},
        }

        # 联系人统计
        contact_db = self._find_contact_db()
        if contact_db:
            key = self._get_key(contact_db)
            if key:
                try:
                    with WcdbSession(db_path=contact_db, enc_key=key) as db:
                        rows = db.query("""
                            SELECT
                                SUM(CASE WHEN username LIKE '%@chatroom' THEN 1 ELSE 0 END) AS groups,
                                SUM(CASE WHEN username LIKE 'gh_%' THEN 1 ELSE 0 END) AS officials,
                                SUM(CASE WHEN username NOT LIKE '%@chatroom' AND username NOT LIKE 'gh_%'
                                    AND COALESCE(flag, 0) & 8 = 0 THEN 1 ELSE 0 END) AS privates
                            FROM contact WHERE username IS NOT NULL AND username != ''
                        """)
                        if rows:
                            result["contacts"]["groups"] = rows[0].get("groups", 0) or 0
                            result["contacts"]["officials"] = rows[0].get("officials", 0) or 0
                            result["contacts"]["privates"] = rows[0].get("privates", 0) or 0
                except Exception:
                    pass

        # 消息统计
        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    tables = db.query(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    )
                    for t in tables:
                        tbl = t["name"]
                        try:
                            rows = db.query(f"SELECT COUNT(*) as cnt FROM {tbl}")
                            cnt = rows[0]["cnt"] if rows else 0
                            result["messages"]["total"] += cnt
                        except Exception:
                            pass
            except Exception:
                continue

        return result

    def get_session_stats(self, session_id: str) -> dict:
        """获取单个会话的统计"""
        result = {
            "session_id": session_id,
            "total_messages": 0,
            "by_type": {},
            "by_sender": {},
            "time_range": {"first": None, "last": None},
        }

        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    tbl = self._find_session_table(db, session_id)
                    if not tbl:
                        continue
                    col = self._name2id_col(db)
                    # 按类型统计（会话表 = 该会话全部消息，不再按发送者 rid 过滤）
                    try:
                        rows = db.query(f"""
                            SELECT local_type, COUNT(*) as cnt
                            FROM {tbl}
                            GROUP BY local_type
                        """)
                        for r in rows:
                            tp = r["local_type"]
                            result["by_type"][tp] = result["by_type"].get(tp, 0) + r["cnt"]
                            result["total_messages"] += r["cnt"]
                    except Exception:
                        pass

                    # 按发送者统计（真实发送者 rid -> username）
                    if col:
                        try:
                            rows = db.query(f"""
                                SELECT n.{col} AS username, COUNT(*) as cnt
                                FROM {tbl} m
                                JOIN name2id n ON m.real_sender_id = n.rowid
                                GROUP BY n.{col}
                            """)
                            for r in rows:
                                result["by_sender"][r["username"]] = r["cnt"]
                        except Exception:
                            pass

                    # 时间范围（全表）
                    try:
                        rows = db.query(f"""
                            SELECT MIN(create_time) as first_ts, MAX(create_time) as last_ts
                            FROM {tbl}
                        """)
                        if rows and rows[0].get("first_ts"):
                            first = rows[0]["first_ts"]
                            last = rows[0]["last_ts"]
                            if first and (not result["time_range"]["first"] or first < result["time_range"]["first"]):
                                result["time_range"]["first"] = first
                            if last and (not result["time_range"]["last"] or last > result["time_range"]["last"]):
                                result["time_range"]["last"] = last
                    except Exception:
                        pass
            except Exception:
                continue

        # 转换时间戳
        if result["time_range"]["first"]:
            result["time_range"]["first"] = datetime.fromtimestamp(
                result["time_range"]["first"]
            ).isoformat()
        if result["time_range"]["last"]:
            result["time_range"]["last"] = datetime.fromtimestamp(
                result["time_range"]["last"]
            ).isoformat()

        return result

    def get_aggregate_stats(self, session_ids: list[str] | None = None) -> dict:
        """获取聚合统计"""
        result = {
            "total_messages": 0,
            "by_session": {},
            "by_type": {},
            "by_hour": {h: 0 for h in range(24)},
            "by_weekday": {d: 0 for d in range(7)},
        }

        wanted = set(session_ids) if session_ids else None
        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    # 表名 md5 反查会话名
                    col = self._name2id_col(db)
                    session_map = {}
                    if col:
                        for r in db.query(f"SELECT {col} AS u FROM name2id"):
                            u = r.get("u")
                            if u:
                                session_map[hashlib.md5(u.encode("utf-8")).hexdigest()] = u
                    tables = db.query(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    )
                    for t in tables:
                        tbl = t["name"]
                        sname = session_map.get(tbl[4:].lower(), tbl)
                        if wanted is not None and sname not in wanted:
                            continue
                        try:
                            rows = db.query(f"""
                                SELECT local_type, create_time, COUNT(*) as cnt
                                FROM {tbl}
                                GROUP BY local_type, create_time
                            """)
                            for r in rows:
                                result["total_messages"] += r["cnt"]
                                result["by_session"][sname] = result["by_session"].get(sname, 0) + r["cnt"]
                                tp = r["local_type"]
                                result["by_type"][tp] = result["by_type"].get(tp, 0) + r["cnt"]
                                if r["create_time"]:
                                    dt = datetime.fromtimestamp(r["create_time"])
                                    result["by_hour"][dt.hour] += r["cnt"]
                                    result["by_weekday"][dt.weekday()] += r["cnt"]
                        except Exception:
                            pass
            except Exception:
                continue

        return result


def main():
    ap = argparse.ArgumentParser(description="统计分析")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex）")
    ap.add_argument("--keys", help="all_keys.json 路径")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("overview", help="总览统计")

    s_p = sub.add_parser("session", help="会话统计")
    s_p.add_argument("session_id", help="会话 ID")

    sub.add_parser("aggregate", help="聚合统计")

    args = ap.parse_args()

    sa = StatsAnalyzer(args.db_dir, args.key, getattr(args, 'keys', None))

    if args.cmd == "overview":
        result = sa.get_overview()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "session":
        result = sa.get_session_stats(args.session_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "aggregate":
        result = sa.get_aggregate_stats()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
