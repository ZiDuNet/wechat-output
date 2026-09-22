#!/usr/bin/env python3
"""search_fts5.py — 基于微信自带 FTS5 索引的全文搜索（直连加密库）

微信 4.x 在 message/message_fts.db 里维护 FTS5 全文索引，分 4 个分片
(message_fts_v4_0..3)。虚拟表使用微信自研分词器 MMFtsTokenizer，
标准 FTS5 未注册该分词器，直接 MATCH 会报 "no such tokenizer: MMFtsTokenizer"。

解决方案：直连加密 message_fts.db，读取 FTS 底层 _content 表 + LIKE 子串匹配
（绕过分词器；实测全库 ~120 万行 LIKE 扫描秒级，性能够用）。
_content 表列映射（已实测验证）：
    c0 = acontent(可搜索正文)   c1 = message_local_id
    c2 = sort_seq               c3 = local_type
    c4 = session_id             c5 = sender_id
    c6 = create_time(unix秒)
session_id / sender_id 是 FTS 库内 name2id 表的 rowid（与 message/contact 库的
name2id 行号【不一致】），必须用 FTS 库自己的 name2id 反查会话/发送者。

CLI:
    python search_fts5.py --db-dir <db_storage> --keys <all_keys.json> --query "关键词"
    python search_fts5.py --db-dir <db_storage> --keys <all_keys.json> --query "关键词" --session "群名" --limit 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from wcdb_core import WcdbSession, load_keys, get_db_key_for_file


class FtsSearcher:
    """基于微信自带 FTS 索引的全文搜索器"""

    def __init__(self, db_dir: str, enc_key: str | None = None, keys_file: str | None = None):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}

    def _get_key(self, db_path: str) -> str | None:
        if self._enc_key:
            return self._enc_key
        return get_db_key_for_file(db_path, self._db_dir, self._keys)

    # ------------------------------------------------------------
    # 数据库定位
    # ------------------------------------------------------------
    def find_fts_db(self) -> str | None:
        """定位微信自带的 message_fts.db（不存在则返回 None）"""
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if name == "message_fts.db" and not name.endswith(("-wal", "-shm")):
                    return os.path.join(root, name)
        return None

    def find_contact_db(self) -> str | None:
        """定位 contact.db（用于显示名解析）"""
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if name == "contact.db" and not name.endswith(("-wal", "-shm")):
                    return os.path.join(root, name)
        return None

    @staticmethod
    def _content_tables(db) -> list[str]:
        """动态探测 FTS 底层 _content 表（微信分片数可能变化）"""
        rows = db.query(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'message_fts_v4_%_content' ORDER BY name"
        )
        return [r["name"] for r in rows]

    @staticmethod
    def _name2id_col(db) -> str | None:
        cols = db.query("PRAGMA table_info(name2id)")
        for c in cols:
            if c["name"] in ("user_name", "username"):
                return c["name"]
        return None

    # ------------------------------------------------------------
    # 映射加载
    # ------------------------------------------------------------
    def load_fts_name2id(self) -> dict[int, str]:
        """FTS 库 name2id：rowid -> username（session_id/sender_id 的权威映射）"""
        dbp = self.find_fts_db()
        if not dbp:
            return {}
        key = self._get_key(dbp)
        if not key:
            return {}
        m: dict[int, str] = {}
        try:
            with WcdbSession(db_path=dbp, enc_key=key) as db:
                col = self._name2id_col(db)
                if not col:
                    return m
                for r in db.query(f"SELECT rowid, {col} AS u FROM name2id"):
                    u = r.get("u")
                    if u:
                        m[r["rowid"]] = u
        except Exception:
            pass
        return m

    def load_display_names(self) -> dict[str, str]:
        """contact.db：username -> 显示名（备注优先，其次昵称）"""
        dbp = self.find_contact_db()
        if not dbp:
            return {}
        key = self._get_key(dbp)
        if not key:
            return {}
        names: dict[str, str] = {}
        try:
            with WcdbSession(db_path=dbp, enc_key=key) as db:
                cols = {c["name"] for c in db.query("PRAGMA table_info(contact)")}
                if "remark" not in cols or "nick_name" not in cols:
                    return names
                for r in db.query("SELECT username, remark, nick_name FROM contact"):
                    disp = (r.get("remark") or "").strip() or (r.get("nick_name") or "").strip()
                    if disp:
                        names[r["username"]] = disp
        except Exception:
            pass
        return names

    def resolve_session(self, session_arg: str) -> tuple[int | None, str]:
        """把 --session 解析为 FTS name2id 的 rowid。

        1) 直接当 username 查 FTS name2id；
        2) 否则当昵称/备注关键词，先在 contact.db 模糊匹配 username，再查 FTS name2id。
        返回 (rowid, 显示名)，找不到返回 (None, 原因说明)。
        """
        fts_n2i = self.load_fts_name2id()
        disp_names = self.load_display_names()
        rev = {v: k for k, v in fts_n2i.items()}  # username -> rowid

        arg = session_arg.strip()
        # 策略1：直接当 username
        if arg in rev:
            return rev[arg], disp_names.get(arg, arg)
        if "@" in arg or arg.startswith("wxid_") or arg in ("filehelper", "weixin", "fmessage"):
            return None, f"「{arg}」不在 FTS name2id 中"

        # 策略2：contact.db 昵称/备注模糊匹配
        cdbp = self.find_contact_db()
        if cdbp:
            key = self._get_key(cdbp)
            if key:
                try:
                    with WcdbSession(db_path=cdbp, enc_key=key) as db:
                        pat = "%" + arg.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                        rows = db.query(
                            "SELECT username, nick_name, remark FROM contact "
                            "WHERE username LIKE ? ESCAPE '\\' OR nick_name LIKE ? ESCAPE '\\' "
                            "OR remark LIKE ? ESCAPE '\\'",
                            (pat, pat, pat),
                        )
                        exact = [r for r in rows
                                 if (r.get("nick_name") or "") == arg or (r.get("remark") or "") == arg]
                        if exact:
                            target = exact[0]
                            note = f"「{arg}」命中 {len(exact)} 个联系人，取第一个: {target['username']}"
                        elif len(rows) == 1:
                            target = rows[0]
                            note = None
                        elif rows:
                            target = rows[0]
                            note = f"「{arg}」模糊命中 {len(rows)} 个联系人，取第一个: {target['username']}"
                        else:
                            target = None
                            note = None
                        if target and target["username"] in rev:
                            if note:
                                print(f"  [i] {note}", file=sys.stderr)
                            return rev[target["username"]], disp_names.get(target["username"], target["username"])
                except Exception:
                    pass
        return None, f"「{arg}」在通讯录/FTS name2id 中未找到"

    # ------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------
    def search(
        self,
        keyword: str,
        session_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        begin_ts: int | None = None,
        end_ts: int | None = None,
    ) -> dict:
        """全文搜索消息（读微信自带 FTS 的 _content 表，LIKE 子串匹配）。

        返回 {"rows": [...], "total": int, "fts_db": str|None, "session": 解析信息}
        """
        dbp = self.find_fts_db()
        if not dbp:
            return {"rows": [], "total": 0, "fts_db": None,
                    "session": None, "warn": "message_fts.db not found or cannot open"}

        key = self._get_key(dbp)
        if not key:
            return {"rows": [], "total": 0, "fts_db": dbp,
                    "session": None, "warn": "找不到 message_fts.db 的密钥"}

        # 会话过滤解析
        session_info = None
        session_rowid = None
        if session_id:
            rowid, disp = self.resolve_session(session_id)
            session_rowid = rowid
            session_info = {"arg": session_id, "rowid": rowid, "display": disp}

        fts_n2i = self.load_fts_name2id()
        disp_names = self.load_display_names()

        like_pat = f"%{keyword}%"
        results = []
        with WcdbSession(db_path=dbp, enc_key=key) as db:
            for tbl in self._content_tables(db):
                sql = (f"SELECT c0 AS acontent, c3 AS local_type, c4 AS session_id, "
                       f"c5 AS sender_id, c6 AS create_time FROM {tbl} WHERE c0 LIKE ?")
                params: list = [like_pat]
                if session_rowid is not None:
                    sql += " AND c4 = ?"
                    params.append(session_rowid)
                if begin_ts:
                    sql += " AND c6 >= ?"
                    params.append(begin_ts)
                if end_ts:
                    sql += " AND c6 <= ?"
                    params.append(end_ts)
                try:
                    rows = db.query(sql, tuple(params))
                except Exception as e:
                    print(f"  [WARN] {tbl} 查询失败: {e}", file=sys.stderr)
                    continue
                for r in rows:
                    s_u = fts_n2i.get(r["session_id"], "?")
                    r_u = fts_n2i.get(r["sender_id"], "?")
                    results.append({
                        "ts": r["create_time"],
                        "session": disp_names.get(s_u, s_u),
                        "session_id_raw": s_u,
                        "sender": disp_names.get(r_u, r_u),
                        "sender_id_raw": r_u,
                        "local_type": r["local_type"],
                        "content": r["acontent"] or "",
                    })

        total = len(results)
        results.sort(key=lambda x: x["ts"] or 0, reverse=True)
        rows = results[offset:offset + limit] if offset else results[:limit]
        return {"rows": rows, "total": total, "fts_db": dbp,
                "session": session_info, "warn": None}


def main():
    ap = argparse.ArgumentParser(description="微信消息全文搜索（读微信自带 FTS 索引）")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex，所有库共用）")
    ap.add_argument("--keys", help="all_keys.json 路径（每个库独立密钥）")
    ap.add_argument("--query", "-q", required=True, help="搜索关键词")
    ap.add_argument("--session", help="限定会话：username 或 群名/昵称")
    ap.add_argument("--limit", type=int, default=50, help="最大返回条数")
    ap.add_argument("--offset", type=int, default=0, help="偏移量")
    ap.add_argument("--ensure-index", action="store_true",
                    help="仅检查微信自带 FTS 索引是否存在（微信自维护，无需创建）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    searcher = FtsSearcher(args.db_dir, args.key, args.keys)

    if args.ensure_index:
        dbp = searcher.find_fts_db()
        if dbp:
            print(f"微信自带 FTS 索引存在: {dbp}")
            print("（索引由微信客户端自维护，无需创建；直接 --query 搜索即可）")
        else:
            print("[!] message_fts.db not found or cannot open")
        return

    t0 = time.time()
    result = searcher.search(
        args.query,
        session_id=args.session,
        limit=args.limit,
        offset=args.offset,
    )
    elapsed = time.time() - t0

    if result["warn"]:
        print(f"[!] {result['warn']}")
        return

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"搜索「{args.query}」: 共 {result['total']} 条命中（{elapsed:.2f}s），显示 {len(result['rows'])} 条")
    if result["session"]:
        si = result["session"]
        print(f"会话过滤: {si['arg']} -> {si['display']} (FTS rowid={si['rowid']})"
              if si["rowid"] is not None else f"会话过滤: {si['arg']} -> {si['display']}")
    print("-" * 70)
    for r in result["rows"]:
        ts = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M") if r["ts"] else "?"
        content = (r["content"] or "").replace("\r", " ").replace("\n", " ").strip()[:120]
        print(f"[{ts}] [{r['session']}] {r['sender']}: {content}")


if __name__ == "__main__":
    main()
