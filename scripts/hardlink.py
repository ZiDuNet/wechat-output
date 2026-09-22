#!/usr/bin/env python3
"""hardlink.py — 硬链接解析模块

统一封装图片/视频的硬链接解析，将 md5/hash 映射到实际文件路径。

用法:
    from hardlink import HardlinkResolver
    resolver = HardlinkResolver(db_dir, enc_key)
    path = resolver.resolve_image("abc123...", account_dir)
    paths = resolver.resolve_video_batch(["md5_1", "md5_2"], db_path)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from wcdb_core import WcdbSession, load_keys, get_db_key_for_file


class HardlinkResolver:
    """硬链接解析器"""

    def __init__(self, db_dir: str, enc_key: str | None = None, keys_file: str | None = None):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}

    def _get_key(self, db_path: str) -> str | None:
        if self._enc_key:
            return self._enc_key
        return get_db_key_for_file(db_path, self._db_dir, self._keys)

    def _find_media_dbs(self) -> list[str]:
        """查找所有媒体数据库"""
        dbs = []
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if "media" in name.lower() and name.endswith(".db") and not name.endswith(("-wal", "-shm")):
                    dbs.append(os.path.join(root, name))
        return sorted(dbs)

    def _find_image_hardlink_table(self, db: WcdbSession) -> str | None:
        """查找图片硬链接表"""
        tables = db.query(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'image_hardlink_info%' ORDER BY name DESC LIMIT 1"
        )
        return tables[0]["name"] if tables else None

    def _find_video_hardlink_table(self, db: WcdbSession) -> str | None:
        """查找视频硬链接表"""
        tables = db.query(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'video_hardlink_info%' ORDER BY name DESC LIMIT 1"
        )
        return tables[0]["name"] if tables else None

    def resolve_image(self, md5: str, account_dir: str | None = None) -> dict | None:
        """解析图片硬链接"""
        for db_path in self._find_media_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    table = self._find_image_hardlink_table(db)
                    if not table:
                        continue
                    rows = db.query(
                        f"SELECT * FROM {table} WHERE lower(md5) = lower(?) LIMIT 1",
                        (md5,)
                    )
                    if rows:
                        result = rows[0]
                        # 尝试定位实际文件
                        if account_dir and "file_path" not in result:
                            result["file_path"] = self._locate_image_file(
                                result, account_dir
                            )
                        return result
            except Exception:
                continue
        return None

    def resolve_image_batch(self, md5_list: list[str], account_dir: str | None = None) -> dict[str, dict]:
        """批量解析图片硬链接"""
        results = {}
        for db_path in self._find_media_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    table = self._find_image_hardlink_table(db)
                    if not table:
                        continue
                    for md5 in md5_list:
                        if md5 in results:
                            continue
                        rows = db.query(
                            f"SELECT * FROM {table} WHERE lower(md5) = lower(?) LIMIT 1",
                            (md5,)
                        )
                        if rows:
                            result = rows[0]
                            if account_dir and "file_path" not in result:
                                result["file_path"] = self._locate_image_file(
                                    result, account_dir
                                )
                            results[md5] = result
            except Exception:
                continue
        return results

    def _locate_image_file(self, info: dict, account_dir: str) -> str | None:
        """根据硬链接信息定位实际图片文件"""
        # 尝试从 hardlink_info 表的字段推断路径
        for key in ["file_path", "path", "dir"]:
            if key in info and info[key]:
                path = info[key]
                if os.path.exists(path):
                    return path
        # 尝试从 md5 构建路径
        md5 = info.get("md5", "")
        if md5 and account_dir:
            attach_root = os.path.join(account_dir, "msg", "attach")
            if os.path.isdir(attach_root):
                for d in os.listdir(attach_root):
                    ad = os.path.join(attach_root, d)
                    if not os.path.isdir(ad):
                        continue
                    for month in os.listdir(ad):
                        md = os.path.join(ad, month, "Img")
                        if os.path.isdir(md):
                            for f in os.listdir(md):
                                if md5.lower() in f.lower():
                                    return os.path.join(md, f)
        return None

    def resolve_video(self, md5: str, db_path: str | None = None) -> dict | None:
        """解析视频硬链接"""
        dbs = [db_path] if db_path else self._find_media_dbs()
        for path in dbs:
            key = self._get_key(path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=path, enc_key=key) as db:
                    table = self._find_video_hardlink_table(db)
                    if not table:
                        continue
                    rows = db.query(
                        f"SELECT * FROM {table} WHERE lower(md5) = lower(?) LIMIT 1",
                        (md5,)
                    )
                    if rows:
                        return rows[0]
            except Exception:
                continue
        return None

    def resolve_video_batch(self, md5_list: list[str], db_path: str | None = None) -> dict[str, dict]:
        """批量解析视频硬链接"""
        results = {}
        dbs = [db_path] if db_path else self._find_media_dbs()
        for path in dbs:
            key = self._get_key(path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=path, enc_key=key) as db:
                    table = self._find_video_hardlink_table(db)
                    if not table:
                        continue
                    for md5 in md5_list:
                        if md5 in results:
                            continue
                        rows = db.query(
                            f"SELECT * FROM {table} WHERE lower(md5) = lower(?) LIMIT 1",
                            (md5,)
                        )
                        if rows:
                            results[md5] = rows[0]
            except Exception:
                continue
        return results

    def list_media_dbs(self) -> list[dict]:
        """列出所有媒体数据库"""
        results = []
        for db_path in self._find_media_dbs():
            key = self._get_key(db_path)
            info = {"path": db_path, "has_key": key is not None}
            if key:
                try:
                    with WcdbSession(db_path=db_path, enc_key=key) as db:
                        tables = db.query("SELECT name FROM sqlite_master WHERE type='table'")
                        info["tables"] = [t["name"] for t in tables]
                except Exception:
                    info["tables"] = []
            results.append(info)
        return results


def main():
    ap = argparse.ArgumentParser(description="硬链接解析")
    sub = ap.add_subparsers(dest="cmd")

    # image 子命令
    i_p = sub.add_parser("image", help="解析图片")
    i_p.add_argument("md5", help="图片 MD5")
    i_p.add_argument("--db-dir", required=True)
    i_p.add_argument("--key")
    i_p.add_argument("--keys")
    i_p.add_argument("--account-dir", help="账号目录")

    # video 子命令
    v_p = sub.add_parser("video", help="解析视频")
    v_p.add_argument("md5", help="视频 MD5")
    v_p.add_argument("--db-dir", required=True)
    v_p.add_argument("--key")
    v_p.add_argument("--keys")
    v_p.add_argument("--db-path", help="指定数据库路径")

    # list-dbs 子命令
    l_p = sub.add_parser("list-dbs", help="列出媒体数据库")
    l_p.add_argument("--db-dir", required=True)
    l_p.add_argument("--key")
    l_p.add_argument("--keys")

    args = ap.parse_args()

    resolver = HardlinkResolver(args.db_dir, args.key, getattr(args, 'keys', None))

    if args.cmd == "image":
        result = resolver.resolve_image(args.md5, getattr(args, 'account_dir', None))
        print(json.dumps(result, ensure_ascii=False, indent=2) if result else "未找到")

    elif args.cmd == "video":
        result = resolver.resolve_video(args.md5, getattr(args, 'db_path', None))
        print(json.dumps(result, ensure_ascii=False, indent=2) if result else "未找到")

    elif args.cmd == "list-dbs":
        dbs = resolver.list_media_dbs()
        print(json.dumps(dbs, ensure_ascii=False, indent=2))

    else:
        ap.print_help()


if __name__ == "__main__":
    main()
