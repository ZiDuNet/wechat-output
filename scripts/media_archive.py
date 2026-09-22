#!/usr/bin/env python3
"""media_archive.py — 媒体批量归档（图片/视频/语音，直连加密库，只读）

按会话（或全部会话）把消息里的媒体文件解析出 md5 → 定位微信本地实际文件
→ 复制/硬链接到输出目录：out/<会话>/image|video|voice/。

不改动微信任何库；仅读取消息表 + media_hardlink 表 + 文件系统。

用法:
    python media_archive.py --db-dir <db_storage> --keys <all_keys.json> \
        --out D:/媒体归档 --session <会话username>
    python media_archive.py --db-dir <db_storage> --keys <all_keys.json> \
        --out D:/媒体归档 --all --hardlink
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict

from msg_reader import MessageReader, msg_body, extract_md5s
from hardlink import HardlinkResolver
from search_fts5 import FtsSearcher

TYPE_LABEL = {3: "image", 43: "video", 34: "voice"}
MD5_COL = {3: "img", 43: "video", 34: "voice"}


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name) or "unknown"


class MediaArchiver:
    """媒体归档器"""

    def __init__(self, db_dir: str, enc_key: str | None = None,
                 keys_file: str | None = None):
        self._reader = MessageReader(db_dir, enc_key, keys_file)
        self._resolver = HardlinkResolver(db_dir, enc_key, keys_file)
        self._fts = FtsSearcher(db_dir, enc_key, keys_file)

    def _session_name_for_table(self, db_path: str, table: str) -> str | None:
        """消息表 Msg_<md5> → 会话 username（name2id 反查）；失败返回 None"""
        key = self._reader._get_key(db_path)
        if not key:
            return None
        from wcdb_core import WcdbSession
        md5 = table[4:].lower()
        try:
            with WcdbSession(db_path=db_path, enc_key=key) as db:
                cols = {c["name"] for c in db.query("PRAGMA table_info(name2id)")}
                col = "user_name" if "user_name" in cols else "username"
                for r in db.query(f"SELECT {col} AS u FROM name2id"):
                    u = r.get("u")
                    if u and hashlib.md5(u.encode("utf-8")).hexdigest() == md5:
                        return u
        except Exception:
            pass
        return None

    def archive(self, out_dir: str, session_id: str | None = None,
                account_dir: str | None = None, media_types: str = "image,video,voice",
                hardlink: bool = False, limit: int | None = None) -> dict:
        """归档媒体。session_id 为空则归档全部会话。"""
        want = set(media_types.split(","))
        copy = os.link if hardlink else shutil.copy2
        counts: dict[str, int] = defaultdict(int)
        copied: dict[str, int] = defaultdict(int)
        errors: list[str] = []

        # display name 解析
        disp_names = self._fts.load_display_names()

        n = 0
        for r in self._reader.iter_messages(session_id):
            if limit and n >= limit:
                break
            n += 1
            lt = r.get("local_type")
            if lt not in TYPE_LABEL:
                continue
            label = TYPE_LABEL[lt]
            if label not in want:
                continue
            body = msg_body(r)
            if not body:
                continue
            md5s = extract_md5s(body).get(MD5_COL[lt], [])
            if not md5s:
                continue

            # 会话名：优先显式 session，否则用 real_sender_id 所在会话表反查
            ses = session_id
            if not ses:
                dbp = r.get("_db_path")
                tbl = r.get("_table")
                ses = self._session_name_for_table(dbp, tbl) if dbp and tbl else None
            ses = ses or "unknown"
            ses_dir = os.path.join(out_dir, safe_name(disp_names.get(ses, ses)), label)
            os.makedirs(ses_dir, exist_ok=True)

            for md in md5s:
                counts[label] += 1
                info = None
                if lt == 3:
                    info = self._resolver.resolve_image(md, account_dir) if account_dir else self._resolver.resolve_image(md)
                elif lt == 43:
                    info = self._resolver.resolve_video(md)
                elif lt == 34:
                    info = self._resolve_voice(md)
                src = (info or {}).get("file_path") or (info or {}).get("path")
                if not src or not os.path.exists(src):
                    continue
                ext = os.path.splitext(src)[1] or ".bin"
                dst = os.path.join(ses_dir, f"{md}{ext}")
                if not os.path.exists(dst):
                    try:
                        copy(src, dst)
                        copied[label] += 1
                    except OSError as e:
                        errors.append(f"{md}: {e}")

        return {"out": out_dir, "messages": n,
                "copied": dict(copied), "seen": dict(counts),
                "errors": errors[:50]}

    def _resolve_voice(self, md5: str):
        """语音文件：位于 Files/msg/voice2/<会话id>/<md5>.amr/.mp3 —— 用 hardlink 表无果时按名字查"""
        from wcdb_core import WcdbSession
        for dbp in self._resolver._find_media_dbs():
            key = self._reader._get_key(dbp)
            if not key:
                continue
            try:
                with WcdbSession(db_path=dbp, enc_key=key) as db:
                    tables = db.query(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name LIKE 'voice_hardlink_info%' ORDER BY name DESC LIMIT 1")
                    if not tables:
                        continue
                    rows = db.query(f"SELECT * FROM {tables[0]['name']} WHERE lower(md5)=lower(?) LIMIT 1",
                                    (md5,))
                    if rows:
                        return rows[0]
            except Exception:
                continue
        return None


def main():
    ap = argparse.ArgumentParser(description="媒体批量归档（图片/视频/语音，只读）")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex，所有库共用）")
    ap.add_argument("--keys", help="all_keys.json 路径（每个库独立密钥）")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--session", help="会话 username（不传则全部会话）")
    ap.add_argument("--account-dir", help="微信账号目录（图片实际文件定位）")
    ap.add_argument("--media-types", default="image,video,voice",
                    help="媒体类型逗号分隔：image,video,voice")
    ap.add_argument("--hardlink", action="store_true", help="硬链接而非复制（需同盘）")
    ap.add_argument("--limit", type=int, help="最多处理的消息条数")
    args = ap.parse_args()

    archiver = MediaArchiver(args.db_dir, args.key, args.keys)
    summary = archiver.archive(args.out, session_id=args.session,
                               account_dir=args.account_dir,
                               media_types=args.media_types,
                               hardlink=args.hardlink, limit=args.limit)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()