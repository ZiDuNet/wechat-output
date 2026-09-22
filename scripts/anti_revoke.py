#!/usr/bin/env python3
"""anti_revoke.py — 消息反撤回模块（可选功能）

安装 SQLite trigger 阻止消息被撤回删除。

⚠️ 警告：此模块会修改数据库，建议先备份！

用法:
    from anti_revoke import AntiRevokeManager
    with AntiRevokeManager(db_dir, enc_key) as arm:
        arm.install(session_id="xxx@chatroom")
        arm.check(session_id="xxx@chatroom")

    # CLI
    python anti_revoke.py --db-dir ... --key ... install --session "xxx@chatroom"
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from wcdb_core import WcdbSession, load_keys, get_db_key_for_file


# 反撤回缓存表 DDL
CACHE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS _weflow_anti_revoke_deleted_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT NOT NULL,
    local_id INTEGER,
    server_id INTEGER,
    local_type INTEGER,
    sort_seq INTEGER,
    real_sender_id INTEGER,
    create_time INTEGER,
    status INTEGER,
    upload_status INTEGER,
    download_status INTEGER,
    server_seq INTEGER,
    origin_source INTEGER,
    source TEXT,
    message_content TEXT,
    compress_content TEXT,
    packed_info_data BLOB,
    WCDB_CT_message_content INTEGER,
    WCDB_CT_source INTEGER,
    deleted_at INTEGER
)
"""

PENDING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS _weflow_anti_revoke_pending (
    session_id TEXT PRIMARY KEY,
    updated_at INTEGER
)
"""


class AntiRevokeManager:
    """反撤回管理器"""

    def __init__(self, db_dir: str, enc_key: str | None = None, keys_file: str | None = None):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}

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

    def _find_message_table(self, db: WcdbSession) -> str | None:
        """查找消息表"""
        tables = db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        )
        return tables[0]["name"] if tables else None

    def install(self, session_id: str | None = None) -> dict:
        """安装反撤回 trigger"""
        results = {"installed": 0, "skipped": 0, "errors": []}
        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    # 创建缓存表
                    db.executescript(CACHE_TABLE_SQL + PENDING_TABLE_SQL)

                    table = self._find_message_table(db)
                    if not table:
                        results["skipped"] += 1
                        continue

                    trigger_name = f"_weflow_anti_revoke_{table}"

                    # 检查是否已安装
                    existing = db.query(
                        "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
                        (trigger_name,)
                    )
                    if existing:
                        results["skipped"] += 1
                        continue

                    # 安装 trigger
                    trigger_sql = f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_name}
                    AFTER DELETE ON {table}
                    BEGIN
                        INSERT INTO _weflow_anti_revoke_deleted_cache
                        (tbl, local_id, server_id, local_type, sort_seq, real_sender_id,
                         create_time, status, upload_status, download_status, server_seq,
                         origin_source, source, message_content, compress_content,
                         packed_info_data, WCDB_CT_message_content, WCDB_CT_source, deleted_at)
                        SELECT
                            '{table}', OLD.local_id, OLD.server_id, OLD.local_type,
                            OLD.sort_seq, OLD.real_sender_id, OLD.create_time,
                            OLD.status, OLD.upload_status, OLD.download_status,
                            OLD.server_seq, OLD.origin_source, OLD.source,
                            OLD.message_content, OLD.compress_content,
                            OLD.packed_info_data, OLD.WCDB_CT_message_content,
                            OLD.WCDB_CT_source, strftime('%s', 'now')
                        WHERE OLD.local_type != 10002;
                    END
                    """
                    db.execute(trigger_sql)
                    results["installed"] += 1
            except Exception as e:
                results["errors"].append(f"{db_path}: {e}")

        return results

    def uninstall(self) -> dict:
        """卸载反撤回 trigger"""
        results = {"uninstalled": 0, "errors": []}
        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    triggers = db.query(
                        "SELECT name FROM sqlite_master WHERE type='trigger' "
                        "AND name LIKE '_weflow_anti_revoke_%'"
                    )
                    for t in triggers:
                        db.execute(f"DROP TRIGGER IF EXISTS {t['name']}")
                        results["uninstalled"] += 1
            except Exception as e:
                results["errors"].append(f"{db_path}: {e}")

        return results

    def check(self, session_id: str | None = None) -> dict:
        """检查反撤回状态"""
        results = {"installed": 0, "pending": 0, "cached": 0}
        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    # 检查 trigger
                    triggers = db.query(
                        "SELECT name FROM sqlite_master WHERE type='trigger' "
                        "AND name LIKE '_weflow_anti_revoke_%'"
                    )
                    results["installed"] += len(triggers)

                    # 检查缓存
                    try:
                        cached = db.query("SELECT COUNT(*) as cnt FROM _weflow_anti_revoke_deleted_cache")
                        results["cached"] += cached[0]["cnt"] if cached else 0
                    except Exception:
                        pass

                    # 检查 pending
                    try:
                        pending = db.query("SELECT COUNT(*) as cnt FROM _weflow_anti_revoke_pending")
                        results["pending"] += pending[0]["cnt"] if pending else 0
                    except Exception:
                        pass
            except Exception:
                continue

        return results

    def restore(self, session_id: str | None = None) -> dict:
        """恢复被撤回的消息"""
        results = {"restored": 0, "errors": []}
        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    table = self._find_message_table(db)
                    if not table:
                        continue

                    # 获取缓存的消息
                    cached = db.query(
                        "SELECT * FROM _weflow_anti_revoke_deleted_cache WHERE tbl = ?",
                        (table,)
                    )

                    for msg in cached:
                        try:
                            # 插入回原表
                            db.execute(f"""
                                INSERT OR IGNORE INTO {table}
                                (server_id, local_type, sort_seq, real_sender_id, create_time,
                                 status, upload_status, download_status, server_seq,
                                 origin_source, source, message_content, compress_content,
                                 packed_info_data, WCDB_CT_message_content, WCDB_CT_source)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                msg.get("server_id"), msg.get("local_type"),
                                msg.get("sort_seq"), msg.get("real_sender_id"),
                                msg.get("create_time"), msg.get("status"),
                                msg.get("upload_status"), msg.get("download_status"),
                                msg.get("server_seq"), msg.get("origin_source"),
                                msg.get("source"), msg.get("message_content"),
                                msg.get("compress_content"), msg.get("packed_info_data"),
                                msg.get("WCDB_CT_message_content"), msg.get("WCDB_CT_source"),
                            ))
                            results["restored"] += 1
                        except Exception as e:
                            results["errors"].append(str(e))

                    # 清理缓存
                    db.execute(
                        "DELETE FROM _weflow_anti_revoke_deleted_cache WHERE tbl = ?",
                        (table,)
                    )
            except Exception as e:
                results["errors"].append(f"{db_path}: {e}")

        return results


def main():
    ap = argparse.ArgumentParser(description="消息反撤回管理")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex）")
    ap.add_argument("--keys", help="all_keys.json 路径")
    sub = ap.add_subparsers(dest="cmd")

    # install
    i_p = sub.add_parser("install", help="安装反撤回 trigger")
    i_p.add_argument("--session", help="限定会话")

    # uninstall
    sub.add_parser("uninstall", help="卸载反撤回 trigger")

    # check
    sub.add_parser("check", help="检查反撤回状态")

    # restore
    r_p = sub.add_parser("restore", help="恢复被撤回的消息")
    r_p.add_argument("--session", help="限定会话")

    args = ap.parse_args()

    arm = AntiRevokeManager(args.db_dir, args.key, getattr(args, 'keys', None))

    if args.cmd == "install":
        results = arm.install(args.session)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.cmd == "uninstall":
        results = arm.uninstall()
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.cmd == "check":
        results = arm.check()
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.cmd == "restore":
        results = arm.restore(args.session)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
