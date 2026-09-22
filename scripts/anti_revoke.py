#!/usr/bin/env python3
"""anti_revoke.py — 消息反撤回模块（可选功能）

安装 SQLite trigger 阻止消息被撤回删除。

⚠️ 警告：此模块会修改数据库，建议先备份！

用法:
    from anti_revoke import AntiRevokeManager
    arm = AntiRevokeManager(db_dir, enc_key)
    arm.install(session_id="xxx@chatroom")
    arm.check(session_id="xxx@chatroom")

    # CLI
    python anti_revoke.py --db-dir ... --key ... install --session "xxx@chatroom"
    python anti_revoke.py --db-dir ... --key ... watch --interval 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from wcdb_core import WcdbSession, load_keys, get_db_key_for_file


# 反撤回 pending 表 DDL（缓存表结构随消息表列动态生成，见 _cache_ddl）
PENDING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS _weflow_anti_revoke_pending (
    session_id TEXT PRIMARY KEY,
    updated_at INTEGER
);
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

    def _msg_columns(self, db: WcdbSession, table: str) -> list[str]:
        """消息表实际列名（随版本自适应，避免硬编码列清单漂移）"""
        cols = [c["name"] for c in db.query(f"PRAGMA table_info({table})")]
        return cols

    def _cache_column_names(self, cols: list[str]) -> list[str]:
        return [c for c in cols if c.lower() not in ("local_id", "server_id")]

    def _cache_ddl(self, cols: list[str]) -> str:
        """据消息表真实列动态生成缓存表 DDL"""
        body = ",\n".join(f"    {c} TEXT" for c in cols)
        return f"""
CREATE TABLE IF NOT EXISTS _weflow_anti_revoke_deleted_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT NOT NULL,
    deleted_at INTEGER,
{body}
);
"""

    def install(self, session_id: str | None = None) -> dict:
        """安装反撤回 trigger"""
        results = {"installed": 0, "skipped": 0, "errors": []}
        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key, readonly=False) as db:
                    db.executescript(PENDING_TABLE_SQL)

                    table = self._find_message_table(db)
                    if not table:
                        results["skipped"] += 1
                        continue

                    # 依据消息表真实列动态建缓存表（版本自适应）
                    cols = self._msg_columns(db, table)
                    db.execute(self._cache_ddl(cols))

                    trigger_name = f"_weflow_anti_revoke_{table}"

                    # 检查是否已安装
                    existing = db.query(
                        "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
                        (trigger_name,)
                    )
                    if existing:
                        results["skipped"] += 1
                        continue

                    # 安装 trigger（动态列，避免硬编码列清单随版本漂移）
                    colnames = ", ".join(cols)
                    old_cols = ", ".join(f"OLD.{c}" for c in cols)
                    cond = "WHERE OLD.local_type != 10002" if "local_type" in cols else ""
                    trigger_sql = f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_name}
                    AFTER DELETE ON {table}
                    BEGIN
                        INSERT INTO _weflow_anti_revoke_deleted_cache (tbl, deleted_at, {colnames})
                        SELECT '{table}', strftime('%s', 'now'), {old_cols}
                        {cond};
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
                with WcdbSession(db_path=db_path, enc_key=key, readonly=False) as db:
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

    def _restore_one(self, db_path: str, key: str) -> dict:
        """恢复单个消息库的撤回缓存（供 restore/watch 复用，列清单动态对齐表结构）"""
        results = {"restored": 0, "errors": []}
        with WcdbSession(db_path=db_path, enc_key=key, readonly=False) as db:
            table = self._find_message_table(db)
            if not table:
                return results
            cols = self._msg_columns(db, table)
            colnames = ", ".join(cols)
            placeholders = ", ".join("?" for _ in cols)
            cached = db.query(
                "SELECT * FROM _weflow_anti_revoke_deleted_cache WHERE tbl = ?",
                (table,)
            )
            for msg in cached:
                try:
                    vals = [msg.get(c) for c in cols]
                    db.execute(
                        f"INSERT OR IGNORE INTO {table} ({colnames}) VALUES ({placeholders})",
                        vals,
                    )
                    results["restored"] += 1
                except Exception as e:
                    results["errors"].append(str(e))

            db.execute(
                "DELETE FROM _weflow_anti_revoke_deleted_cache WHERE tbl = ?",
                (table,)
            )
        return results

    def restore(self, session_id: str | None = None) -> dict:
        """恢复被撤回的消息"""
        results = {"restored": 0, "errors": []}
        for db_path in self._find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                r = self._restore_one(db_path, key)
                results["restored"] += r["restored"]
                results["errors"].extend(r["errors"])
            except Exception as e:
                results["errors"].append(f"{db_path}: {e}")

        return results

    def watch(self, interval: float = 2.0, once: bool = False,
              state_file: str | None = None,
              max_events: int | None = None) -> dict:
        """实时监听 + 自动恢复撤回消息。

        基于 deleted_at 水位增量处理；--state 保存水位，重启续跑。
        需要先 install() 安装 trigger。返回事件列表。
        """
        state: dict[str, int] = {}
        if state_file and os.path.exists(state_file):
            with open(state_file, "r", encoding="utf-8") as f:
                try:
                    state = json.load(f)
                except Exception:
                    state = {}

        events: list[dict] = []
        restored_total = 0
        ticks = 0
        try:
            while max_events is None or ticks < max_events:
                ticks += 1
                for db_path in self._find_message_dbs():
                    key = self._get_key(db_path)
                    if not key:
                        continue
                    wm = state.get(db_path, 0)
                    try:
                        with WcdbSession(db_path=db_path, enc_key=key) as db:
                            rows = db.query(
                                "SELECT COUNT(*) AS c, MAX(deleted_at) AS m "
                                "FROM _weflow_anti_revoke_deleted_cache "
                                "WHERE deleted_at > ?",
                                (wm,)
                            )
                        if rows and rows[0]["c"]:
                            r = self._restore_one(db_path, key)
                            newm = rows[0]["m"] or wm
                            state[db_path] = max(int(wm), int(newm))
                            restored_total += r["restored"]
                            events.append({
                                "db": db_path,
                                "time": datetime.now().isoformat(),
                                "restored": r["restored"],
                                "errors": r["errors"],
                            })
                            print(f"[{datetime.now():%H:%M:%S}] 恢复 {r['restored']} 条撤回: "
                                  f"{db_path}", file=sys.stderr)
                    except Exception as e:
                        events.append({"db": db_path,
                                       "time": datetime.now().isoformat(),
                                       "error": str(e)})
                if once:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        finally:
            if state_file:
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)

        return {"events": events, "restored": restored_total, "ticks": ticks,
                "state": state}


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

    # watch
    w_p = sub.add_parser("watch", help="实时监听并自动恢复撤回消息（需先 install）")
    w_p.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数")
    w_p.add_argument("--once", action="store_true", help="只跑一轮")
    w_p.add_argument("--state", help="水位文件路径（断电续跑）")
    w_p.add_argument("--max-events", type=int, help="最多处理事件批次数后退出")

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
    elif args.cmd == "watch":
        results = arm.watch(interval=args.interval, once=args.once,
                            state_file=args.state, max_events=args.max_events)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
