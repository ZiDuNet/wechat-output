#!/usr/bin/env python3
"""db_health.py — 数据库健康检查模块

检查微信数据库的完整性、分片状态、大小等。

用法:
    from db_health import DbHealthChecker
    with DbHealthChecker(db_dir, enc_key) as checker:
        report = checker.full_check()
        print(report)

    # CLI
    python db_health.py --db-dir ... --key ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from wcdb_core import WcdbSession, find_db_files, load_keys, get_db_key_for_file, backend_name


class DbHealthChecker:
    """数据库健康检查器"""

    def __init__(self, db_dir: str, enc_key: str | None = None, keys_file: str | None = None):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}

    def _get_key(self, db_path: str) -> str | None:
        if self._enc_key:
            return self._enc_key
        return get_db_key_for_file(db_path, self._db_dir, self._keys)

    def check_db(self, db_path: str) -> dict:
        """检查单个数据库"""
        result = {
            "path": db_path,
            "rel": os.path.relpath(db_path, self._db_dir),
            "size_mb": round(os.path.getsize(db_path) / 1024 / 1024, 2),
            "mtime": datetime.fromtimestamp(os.path.getmtime(db_path)).isoformat(),
            "status": "unknown",
            "tables": 0,
            "rows": {},
            "errors": [],
        }

        key = self._get_key(db_path)
        if not key:
            result["status"] = "no_key"
            result["errors"].append("未找到密钥")
            return result

        try:
            with WcdbSession(db_path=db_path, enc_key=key) as db:
                # 检查表
                tables = db.query("SELECT name FROM sqlite_master WHERE type='table'")
                result["tables"] = len(tables)
                result["table_names"] = [t["name"] for t in tables]

                # 检查关键表的行数
                for t in tables:
                    name = t["name"]
                    if any(k in name.lower() for k in ["message", "contact", "session", "sns"]):
                        try:
                            rows = db.query(f"SELECT COUNT(*) as cnt FROM {name}")
                            result["rows"][name] = rows[0]["cnt"] if rows else 0
                        except Exception:
                            pass

                # 完整性检查
                integrity = db.query("PRAGMA integrity_check")
                if integrity and integrity[0].get("integrity_check") != "ok":
                    result["status"] = "corrupt"
                    result["errors"].append(f"完整性检查失败: {integrity[0]}")
                else:
                    result["status"] = "ok"

        except Exception as e:
            result["status"] = "error"
            result["errors"].append(str(e))

        return result

    def full_check(self) -> dict:
        """完整健康检查"""
        report = {
            "timestamp": datetime.now().isoformat(),
            "backend": backend_name(),
            "db_dir": self._db_dir,
            "categories": {},
            "summary": {
                "total_dbs": 0,
                "ok": 0,
                "errors": 0,
                "no_key": 0,
                "total_size_mb": 0,
            },
        }

        cats = find_db_files(self._db_dir)
        for cat, paths in cats.items():
            if not paths:
                continue
            cat_report = {
                "count": len(paths),
                "dbs": [],
                "total_size_mb": 0,
            }
            for path in sorted(paths):
                db_info = self.check_db(path)
                cat_report["dbs"].append(db_info)
                cat_report["total_size_mb"] += db_info["size_mb"]
                report["summary"]["total_dbs"] += 1
                report["summary"]["total_size_mb"] += db_info["size_mb"]
                if db_info["status"] == "ok":
                    report["summary"]["ok"] += 1
                elif db_info["status"] == "no_key":
                    report["summary"]["no_key"] += 1
                else:
                    report["summary"]["errors"] += 1
            cat_report["total_size_mb"] = round(cat_report["total_size_mb"], 2)
            report["categories"][cat] = cat_report

        report["summary"]["total_size_mb"] = round(report["summary"]["total_size_mb"], 2)
        return report

    def quick_check(self) -> dict:
        """快速检查（只看文件大小和数量）"""
        cats = find_db_files(self._db_dir)
        result = {}
        for cat, paths in cats.items():
            if paths:
                total_size = sum(os.path.getsize(p) for p in paths)
                result[cat] = {
                    "count": len(paths),
                    "total_size_mb": round(total_size / 1024 / 1024, 2),
                }
        return result


def main():
    ap = argparse.ArgumentParser(description="数据库健康检查")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex）")
    ap.add_argument("--keys", help="all_keys.json 路径")
    ap.add_argument("--quick", action="store_true", help="快速检查")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    checker = DbHealthChecker(args.db_dir, args.key, args.keys)

    if args.quick:
        report = checker.quick_check()
    else:
        report = checker.full_check()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if args.quick:
            print("快速检查:")
            for cat, info in report.items():
                print(f"  {cat}: {info['count']} 个库, {info['total_size_mb']} MB")
        else:
            print(f"健康检查报告 ({report['timestamp']})")
            print(f"后端: {report['backend']}")
            print(f"总计: {report['summary']['total_dbs']} 个库, "
                  f"{report['summary']['total_size_mb']} MB")
            print(f"  正常: {report['summary']['ok']}")
            print(f"  无密钥: {report['summary']['no_key']}")
            print(f"  错误: {report['summary']['errors']}")
            print()
            for cat, info in report.get("categories", {}).items():
                print(f"{cat} ({info['count']} 个, {info['total_size_mb']} MB):")
                for db in info["dbs"]:
                    status = "✓" if db["status"] == "ok" else "✗"
                    print(f"  {status} {db['rel']} ({db['size_mb']} MB)")
                    if db["errors"]:
                        for e in db["errors"]:
                            print(f"    错误: {e}")


if __name__ == "__main__":
    main()
