#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pay_export.py — 微信 4.x 复合消息明细数据接口（转账/红包/小程序/视频号/链接，直连只读）

基于 appmsg_parser.py 对 local_type 49 的深度解析，输出单会话的结构化明细。

用法:
    # 数据接口（推荐，JSON）
    from pay_export import PayExporter
    ex = PayExporter(db_dir, keys_file="all_keys.json")
    rows = ex.fetch(session="北清路TT", since_ts=..., until_ts=..., kinds=None)
    # rows: [{kind, time, sender, amount, memo, sub_type, payer, receiver,
    #         transfer_id, title, quote, url, ...}]  kind ∈
    #        transfer/redpacket/miniprogram/finder/link/reply/video_share/canvas

    # CLI 数据输出（默认 JSON 到 stdout；--out 写文件）
    python pay_export.py --db-dir <db_storage> --keys <all_keys.json> --session "北清路TT"
    python pay_export.py --db-dir ... --keys ... --session wxid_xxx \
        --last 3m --kind transfer,redpacket
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msg_reader import MessageReader, msg_body  # noqa: E402
from appmsg_parser import parse_appmsg, pay_label  # noqa: E402
from media_common import add_time_args, parse_time_range  # noqa: E402

KIND_CN = {
    "transfer": "转账", "redpacket": "红包", "miniprogram": "小程序",
    "finder": "视频号", "link": "链接", "reply": "引用",
    "video_share": "视频", "canvas": "画布",
}


class PayExporter:
    """复合消息（49 类 appmsg）明细数据接口（直连加密库，只读）。

    from pay_export import PayExporter
    ex = PayExporter(db_dir, keys_file="all_keys.json")
    rows = ex.fetch(session, since_ts=None, until_ts=None, kinds=None)
    """

    def __init__(self, db_dir: str, keys_file: str | None = None,
                 enc_key: str | None = None):
        self.db_dir = db_dir
        self.keys_file = keys_file
        self.reader = MessageReader(db_dir, enc_key=enc_key, keys_file=keys_file)
        self._names = None

    def _contact_names(self) -> dict:
        if self._names is not None:
            return self._names
        from wcdb_core import load_keys, get_db_key_for_file, WcdbSession
        keys = load_keys(self.keys_file) if self.keys_file else {}
        names = {}
        for root, _d, files in os.walk(self.db_dir):
            for name in files:
                if name.startswith("contact") and name.endswith(".db") \
                        and not name.endswith(("-wal", "-shm")) and "fts" not in name:
                    path = os.path.join(root, name)
                    key = get_db_key_for_file(path, self.db_dir, keys)
                    if not key:
                        continue
                    try:
                        with WcdbSession(db_path=path, enc_key=key) as db:
                            for r in db.query("SELECT username, nick_name, remark FROM contact"):
                                u = r.get("username") or ""
                                if not u:
                                    continue
                                d = (r.get("remark") or "").strip() or (r.get("nick_name") or "").strip()
                                if d:
                                    names[u] = d
                    except Exception:
                        pass
        self._names = names
        return names

    def _resolve_session(self, session: str) -> str:
        """显示名/昵称 → username（username 直接返回，多候选报错）"""
        s = session.strip()
        if s.endswith("@chatroom") or s.startswith("wxid_") or "@openim" in s:
            return s
        names = self._contact_names()
        hits = [u for u, n in names.items() if n and s in n]
        if not hits:
            return s
        if len(hits) > 1:
            raise ValueError(f"「{session}」命中 {len(hits)} 个候选，请用 username 精确指定: "
                             + ", ".join(hits[:8]))
        return hits[0]

    def fetch(self, session: str, since_ts: int | None = None,
              until_ts: int | None = None, kinds: set[str] | None = None) -> list[dict]:
        """返回复合消息明细（按时间升序）。

        每项至少含 kind/time/sender；kind 附加字段见 appmsg_parser.parse_appmsg。
        """
        session = self._resolve_session(session)
        names = self._contact_names()
        rows = []
        for m in self.reader.iter_messages(session_id=session,
                                           begin_ts=since_ts, end_ts=until_ts,
                                           local_types={49}):
            info = parse_appmsg(msg_body(m))
            if not info:
                continue
            k = info.get("kind")
            if kinds and k not in kinds:
                continue
            src_u = m.get("_sender_user") or ""
            row = {
                "kind": k,
                "kind_cn": KIND_CN.get(k, k),
                "time": m.get("create_time") or 0,
                "time_cn": datetime.fromtimestamp(m.get("create_time") or 0)
                .strftime("%Y-%m-%d %H:%M"),
                "sender": names.get(src_u, "我" if not src_u else src_u),
                "label": pay_label(info),
            }
            row.update({kk: vv for kk, vv in info.items() if kk != "kind"})
            rows.append(row)
        rows.sort(key=lambda x: x["time"])
        return rows


def main():
    ap = argparse.ArgumentParser(description="微信复合消息明细数据接口（转账/红包/小程序/视频号）")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--keys", help="all_keys.json 路径")
    ap.add_argument("--key", help="64hex 统一密钥（无 keys 文件时）")
    ap.add_argument("--session", required=True, help="会话 username 或 群名/昵称")
    ap.add_argument("--out", help="输出文件（默认 stdout JSON）")
    add_time_args(ap)
    ap.add_argument("--kind", help="逗号分隔过滤类型，如 transfer,redpacket（默认全部）")
    ap.add_argument("--limit", type=int, default=0, help="最多返回条数（0=全部）")
    args = ap.parse_args()

    begin_ts, end_ts = parse_time_range(args.since, args.until, args.last)
    kinds = {k.strip() for k in (args.kind or "").split(",") if k.strip()} or None

    ex = PayExporter(args.db_dir, keys_file=args.keys, enc_key=args.key)
    rows = ex.fetch(args.session, since_ts=begin_ts, until_ts=end_ts, kinds=kinds)
    if args.limit:
        rows = rows[-args.limit:]

    stats = {}
    for r in rows:
        stats[r["kind"]] = stats.get(r["kind"], 0) + 1

    payload = {"session": args.session, "total": len(rows),
               "stats": {KIND_CN.get(k, k): v for k, v in stats.items()},
               "rows": rows}
    out = args.out or "-"
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if out == "-":
        print(text)
    else:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"复合消息数据: {len(rows)} 条 -> {out}", file=sys.stderr)
    print(f"统计: {json.dumps(payload['stats'], ensure_ascii=False)}", file=sys.stderr)


if __name__ == "__main__":
    main()
