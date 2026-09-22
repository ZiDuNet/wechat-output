#!/usr/bin/env python3
"""msg_reader.py — 通用消息读取（直连加密库读 Msg_<md5> 分表，只读）

被封装的公共能力：
- 定位消息库（message*.db）与会话分表（wcdb_core.find_session_table）
- 逐分片迭代消息行，防跨库重复
- zstd 富文本解压、消息正文类型标签
- 从消息 XML 正文提取图片/视频/语音 md5

用法:
    from msg_reader import MessageReader
    reader = MessageReader(db_dir, enc_key, keys_file)
    for msg in reader.iter_messages(session_id, begin_ts, end_ts):
        print(msg)
"""
from __future__ import annotations

import os
import re
import sys

from wcdb_core import WcdbSession, find_session_table, load_keys, get_db_key_for_file


def try_zstd(data) -> str | None:
    """富文本消息正文是 zstd 压缩的 XML；失败返回 None"""
    if not data:
        return None
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data).decode("utf-8", errors="replace")
    except Exception:
        return None


def msg_body(r: dict) -> str:
    """取消息正文：优先 WCDB 压缩列判定，bytes 先试 zstd"""
    content = r.get("message_content")
    if isinstance(content, bytes):
        dec = try_zstd(content)
        if dec:
            return dec
        return content.decode("utf-8", errors="replace")
    if content is None:
        return ""
    return str(content)


def extract_md5s(text: str) -> dict:
    """从消息正文 XML 提取媒体 md5：{"img": [...], "video": [...], "voice": [...]}"""
    out: dict[str, list[str]] = {"img": [], "video": [], "voice": []}
    if not text:
        return out
    for m in re.finditer(r"<img[^>]*>", text, re.S | re.I):
        md5 = re.search(r'\bmd5\s*=\s*["\']([0-9a-fA-F]{32})["\']', m.group(0))
        if md5:
            out["img"].append(md5.group(1).lower())
    for m in re.finditer(r"<videomsg[^>]*>.*?</videomsg>", text, re.S | re.I):
        md5 = re.search(r'\bmd5\s*=\s*["\']([0-9a-fA-F]{32})["\']', m.group(0))
        if md5:
            out["video"].append(md5.group(1).lower())
    for m in re.finditer(r"<voicemsg[^>]*>.*?</voicemsg>", text, re.S | re.I):
        md5 = re.search(r'\bmd5\s*=\s*["\']([0-9a-fA-F]{32})["\']', m.group(0))
        if md5:
            out["voice"].append(md5.group(1).lower())
    return out


LOCAL_TYPE_LABEL = {
    1: "text", 3: "image", 34: "voice", 43: "video", 47: "emoji",
    49: "app", 10002: "system",
}


class MessageReader:
    """按会话读取全部消息分片（只读直连加密库）"""

    def __init__(self, db_dir: str, enc_key: str | None = None,
                 keys_file: str | None = None):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}
        self._n2i_cache: dict[str, dict[int, str]] = {}

    def _get_key(self, db_path: str) -> str | None:
        if self._enc_key:
            return self._enc_key
        return get_db_key_for_file(db_path, self._db_dir, self._keys)

    def find_message_dbs(self) -> list[str]:
        dbs = []
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if name.startswith("message") and name.endswith(".db") \
                        and not name.endswith(("-wal", "-shm")):
                    dbs.append(os.path.join(root, name))
        return sorted(dbs)

    def iter_messages(self, session_id: str | None = None,
                      begin_ts: int | None = None, end_ts: int | None = None,
                      local_types: set[int] | None = None):
        """迭代消息行；跨分片去重（local_id 唯一，安全）。"""
        seen: set[tuple] = set()
        for db_path in self.find_message_dbs():
            key = self._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    if session_id:
                        tbl = find_session_table(db, session_id)
                        tables = [tbl] if tbl else []
                    else:
                        tables = [t["name"] for t in db.query(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name LIKE 'Msg_%'")]
                    for tbl in tables:
                        for r in self._iter_table(db, tbl, begin_ts, end_ts, local_types):
                            r["_db_path"] = db_path
                            r["_table"] = tbl
                            r["_sender_user"] = self._n2i_of(db_path).get(
                                r.get("real_sender_id"))
                            k = (r.get("local_id"), r.get("server_id"), r.get("sort_seq"))
                            if k in seen:
                                continue
                            seen.add(k)
                            yield r
            except Exception as e:
                print(f"  [WARN] {db_path} 读取失败: {e}", file=sys.stderr)

    def _iter_table(self, db: WcdbSession, tbl: str, begin_ts, end_ts, local_types):
        sql = (f"SELECT local_id, server_id, local_type, create_time, sort_seq, "
               f"status, real_sender_id, message_content "
               f"FROM {tbl} WHERE 1=1")
        params: list = []
        if begin_ts:
            sql += " AND create_time >= ?"
            params.append(begin_ts)
        if end_ts:
            sql += " AND create_time <= ?"
            params.append(end_ts)
        if local_types:
            sql += " AND (local_type & 255) IN (%s)" % ",".join("?" * len(local_types))
            params.extend(sorted(local_types))
        sql += " ORDER BY sort_seq"
        try:
            for r in db.query(sql, tuple(params)):
                yield r
        except Exception as e:
            print(f"  [WARN] {tbl} 查询失败: {e}", file=sys.stderr)

    def _n2i_of(self, db_path: str) -> dict[int, str]:
        """该库的 rid -> username（带缓存；rid 局部于库不可跨库复用）"""
        if db_path in self._n2i_cache:
            return self._n2i_cache[db_path]
        m = self.load_name2id(db_path)
        self._n2i_cache[db_path] = m
        return m

    def load_name2id(self, db_path: str) -> dict[int, str]:
        """rid -> username（消息库 name2id，real_sender_id 权威映射）"""
        key = self._get_key(db_path)
        if not key:
            return {}
        m: dict[int, str] = {}
        try:
            with WcdbSession(db_path=db_path, enc_key=key) as db:
                cols = {c["name"] for c in db.query("PRAGMA table_info(name2id)")}
                col = "user_name" if "user_name" in cols else "username"
                for r in db.query(f"SELECT rowid, {col} AS u FROM name2id"):
                    if r.get("u"):
                        m[r["rowid"]] = r["u"]
        except Exception:
            pass
        return m