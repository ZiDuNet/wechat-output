#!/usr/bin/env python3
"""wx_accounts.py — 多账号识别与隔离（纯只读，不碰库）

微信 4.x 多账号布局：父目录下每个账号一个子目录，内含 db_storage。
本工具扫描 `--db-dir` 下的账号子目录：
- list：列出账号（目录名、库数量、体积、密钥覆盖）
- isolate：按账号过滤 all_keys.json，产出一个只含该账号密钥的子集，
  供其它模块以该账号 db_storage 作为 --db-dir 时使用。

用法:
    python wx_accounts.py --db-dir <xwechat_files> --keys all_keys.json list
    python wx_accounts.py --db-dir <xwechat_files> --keys all_keys.json isolate \
        --account <账号目录名> --out keys_账号A.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _is_db_dir(path: str) -> bool:
    """目录内是否存在任一 .db（递归）"""
    for root, _dirs, files in os.walk(path):
        for f in files:
            if f.endswith(".db") and not f.endswith(("-wal", "-shm")):
                return True
    return False


def scan_accounts(db_dir: str) -> list[dict]:
    """扫描账号。返回 [{name, db_dir, db_count, size_mb}]"""
    db_dir = os.path.abspath(db_dir)
    accounts: list[dict] = []

    # 1) db_dir 自身就是 db_storage（直接含 db）
    if _is_db_dir(db_dir):
        accounts.append(_account_for(os.path.dirname(db_dir), db_dir, "root"))
    # 2) db_dir/db_storage 单账号
    nested = os.path.join(db_dir, "db_storage")
    if os.path.isdir(nested) and _is_db_dir(nested):
        accounts.append(_account_for(db_dir, nested, "single"))

    # 3) 子目录各自是账号（含 db 或 db_storage）
    for name in sorted(os.listdir(db_dir)):
        sub = os.path.join(db_dir, name)
        if not os.path.isdir(sub):
            continue
        if name == "db_storage":
            continue
        cand = os.path.join(sub, "db_storage")
        if os.path.isdir(cand) and _is_db_dir(cand):
            accounts.append(_account_for(sub, cand, name))
        elif _is_db_dir(sub):
            accounts.append(_account_for(sub, sub, name))

    # 去重（同一 db_dir 只留一个）
    seen: set[str] = set()
    uniq = []
    for a in accounts:
        if a["db_dir"] in seen:
            continue
        seen.add(a["db_dir"])
        uniq.append(a)
    return uniq


def _account_for(parent: str, db_dir: str, name: str) -> dict:
    db_count = 0
    total = 0
    for root, _dirs, files in os.walk(db_dir):
        for f in files:
            if f.endswith(".db") and not f.endswith(("-wal", "-shm")):
                db_count += 1
                total += os.path.getsize(os.path.join(root, f))
    return {
        "name": name,
        "parent": parent,
        "db_dir": db_dir,
        "db_count": db_count,
        "size_mb": round(total / 1024 / 1024, 2),
    }


def load_raw_keys(keys_file: str) -> dict:
    with open(keys_file, encoding="utf-8") as f:
        return json.load(f)


def list_accounts(db_dir: str, keys_file: str | None) -> dict:
    accounts = scan_accounts(db_dir)
    keys = load_raw_keys(keys_file) if keys_file else {}
    rel_db_dir = os.path.normpath(db_dir)
    for a in accounts:
        prefix = os.path.relpath(a["db_dir"], rel_db_dir).replace(os.sep, "/")
        a["key_entries"] = sum(
            1 for rel in keys if prefix == "." and "/" not in rel
            or rel.startswith(prefix + "/") or rel == prefix)
        a["has_keys"] = a["key_entries"] > 0
    return {"db_dir": db_dir, "accounts": accounts,
            "total_accounts": len(accounts)}


def isolate(db_dir: str, keys_file: str, account_name: str,
            out_file: str) -> dict:
    accounts = scan_accounts(db_dir)
    target = next((a for a in accounts if a["name"] == account_name), None)
    if not target:
        matching = [a["name"] for a in accounts]
        raise SystemExit(f"账号「{account_name}」不存在。可用账号: {matching or '(无)'}")

    keys = load_raw_keys(keys_file)
    prefix = os.path.relpath(target["db_dir"], os.path.normpath(db_dir)).replace(os.sep, "/")
    filtered = {}
    for rel, info in keys.items():
        rel_n = rel.replace(os.sep, "/")
        if prefix == ".":
            if "/" not in rel_n:
                filtered[rel] = info
        elif rel_n == prefix or rel_n.startswith(prefix + "/"):
            filtered[rel] = info
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)
    return {"account": account_name, "db_dir": target["db_dir"],
            "out": out_file, "key_entries": len(filtered)}


def main():
    ap = argparse.ArgumentParser(description="多账号识别与隔离")
    ap.add_argument("--db-dir", required=True, help="账号父目录或 db_storage")
    ap.add_argument("--keys", help="all_keys.json 路径")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出账号")
    iso = sub.add_parser("isolate", help="按账号过滤 keys")
    iso.add_argument("--account", required=True, help="账号目录名")
    iso.add_argument("--out", required=True, help="输出 keys 文件路径")

    args = ap.parse_args()

    if args.cmd == "list":
        result = list_accounts(args.db_dir, args.keys)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"扫描目录: {result['db_dir']}  共 {result['total_accounts']} 个账号")
            for a in result["accounts"]:
                print(f"  [{a['name']}]  {a['db_dir']}")
                print(f"      库 {a['db_count']} 个, {a['size_mb']} MB, "
                      f"密钥条目 {a['key_entries']}")
    elif args.cmd == "isolate":
        result = isolate(args.db_dir, args.keys, args.account, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()