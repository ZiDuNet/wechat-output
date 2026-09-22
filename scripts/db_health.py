#!/usr/bin/env python3
"""db_health.py — 数据库健康检查模块

检查微信数据库的完整性、分片状态、大小等。

用法:
    from db_health import DbHealthChecker
    checker = DbHealthChecker(db_dir, enc_key)
    report = checker.full_check()       # 或 .quick_check()
    checker.save_snapshot("health.json")
    diff = checker.diff_snapshot("health.json")

    # CLI（含定期巡检）
    python db_health.py --db-dir ... --key ... --save health.json
    python db_health.py --db-dir ... --key ... --watch health.json --interval 300
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

    def save_snapshot(self, path: str) -> dict:
        """完整检查并存快照到文件（供 diff/watch 基线）"""
        report = self.full_check()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return {"saved": path, "timestamp": report["timestamp"],
                "total_dbs": report["summary"]["total_dbs"]}

    def diff_snapshot(self, path: str) -> dict:
        """与历史快照比对，报告新增/消失/状态变化/体积变化"""
        with open(path, "r", encoding="utf-8") as f:
            old = json.load(f)
        new = self.full_check()

        def db_index(report: dict) -> dict[str, dict]:
            idx: dict[str, dict] = {}
            for cat, info in report.get("categories", {}).items():
                for d in info.get("dbs", []):
                    idx[d["rel"]] = d
            return idx

        old_idx = db_index(old)
        new_idx = db_index(new)
        added = [r for r in sorted(set(new_idx) - set(old_idx))]
        removed = [r for r in sorted(set(old_idx) - set(new_idx))]

        changed = []
        for rel in sorted(set(old_idx) & set(new_idx)):
            o, n = old_idx[rel], new_idx[rel]
            reasons = []
            if o.get("status") != n.get("status"):
                reasons.append(f"状态 {o.get('status')}→{n.get('status')}")
            if abs(n.get("size_mb", 0) - o.get("size_mb", 0)) >= 1.0:
                reasons.append(f"体积 {o.get('size_mb')}MB→{n.get('size_mb')}MB")
            if o.get("rows") != n.get("rows"):
                reasons.append("关键表行数变化")
            if reasons:
                changed.append({"rel": rel, "reasons": reasons,
                                "new_mtime": n.get("mtime"),
                                "new_size_mb": n.get("size_mb")})
        return {
            "old_snapshot": path,
            "new_timestamp": new["timestamp"],
            "added": [{"rel": r, **{k: new_idx[r].get(k) for k in ("size_mb", "mtime")}} for r in added],
            "removed": [{"rel": r, **{k: old_idx[r].get(k) for k in ("size_mb", "mtime")}} for r in removed],
            "changed": changed,
            "unchanged": len(set(old_idx) & set(new_idx)) - len(changed),
        }


def main():
    ap = argparse.ArgumentParser(description="数据库健康检查 + 定期巡检")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex）")
    ap.add_argument("--keys", help="all_keys.json 路径")
    ap.add_argument("--quick", action="store_true", help="快速检查")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--save", metavar="PATH", help="完整检查并存快照到文件")
    ap.add_argument("--diff", metavar="PATH", help="与已存快照比对，输出变化")
    ap.add_argument("--watch", metavar="PATH",
                    help="定期巡检：首次存快照到 PATH，之后每 --interval 秒比对并告警")
    ap.add_argument("--interval", type=float, default=60.0, help="watch 轮询间隔秒数")
    args = ap.parse_args()

    checker = DbHealthChecker(args.db_dir, args.key, args.keys)

    if args.watch:
        if not os.path.exists(args.watch):
            checker.save_snapshot(args.watch)
            print(f"基线快照已保存: {args.watch}")
        else:
            print(f"用已存快照作基线: {args.watch}")
        print("开始巡检（Ctrl+C 结束）...")
        try:
            while True:
                time.sleep(args.interval)
                diff = checker.diff_snapshot(args.watch)
                if args.json:
                    print(json.dumps(diff, ensure_ascii=False))
                else:
                    _print_diff(diff, "巡检")
                checker.save_snapshot(args.watch)  # 逐步更新基线
        except KeyboardInterrupt:
            print("\n巡检结束")
            return

    if args.diff:
        diff = checker.diff_snapshot(args.diff)
        if args.json:
            print(json.dumps(diff, ensure_ascii=False, indent=2))
        else:
            _print_diff(diff, "快照比对")
        return

    if args.save:
        saved = checker.save_snapshot(args.save)
        print(f"快照已保存: {saved['saved']}  (共 {saved['total_dbs']} 个库, "
              f"{saved['timestamp']})")
        return

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


def _print_diff(diff: dict, title: str):
    print(f"\n[{title}] 变化汇总 ({diff.get('new_timestamp', '')})")
    total = len(diff.get("added", [])) + len(diff.get("removed", [])) + len(diff.get("changed", []))
    print(f"  未变化: {diff.get('unchanged', 0)}   新增: {len(diff.get('added', []))}   "
          f"消失: {len(diff.get('removed', []))}   "
          f"变化: {len(diff.get('changed', []))}")
    for a in diff.get("added", []):
        print(f"  [+] 新增 {a['rel']} ({a.get('size_mb', '?')} MB)")
    for r in diff.get("removed", []):
        print(f"  [-] 消失 {r['rel']}")
    for c in diff.get("changed", []):
        print(f"  [~] {c['rel']}: {'; '.join(c['reasons'])}")


if __name__ == "__main__":
    main()
