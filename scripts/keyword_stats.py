#!/usr/bin/env python3
"""keyword_stats.py — 热词统计（读微信自带 FTS 索引，只读）

基于 search_fts5 的 _content 扫描，按「天」或「会话」汇总关键词词频，
输出 JSON 或 Markdown 报告。可用作：本周热词、话题趋势、核心议题识别。

分词规则（零第三方依赖）：
- 连续 CJK 2 字及以上成词
- 连续 ASCII 字母 3 位及以上成词（小写）
- 剔除数字、纯标点与内置停用词

CLI:
    python keyword_stats.py --db-dir <db_storage> --keys <all_keys.json> \
        --days 30 --top 20 --group-by day
    python keyword_stats.py --db-dir <db_storage> --keys <all_keys.json> \
        --session <群名/昵称/username> --out report.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

from search_fts5 import FtsSearcher

STOPWORDS = {
    "一个", "我们", "你们", "他们", "自己", "这样", "那样", "什么", "怎么", "为什么",
    "没有", "还有", "因为", "所以", "但是", "而且", "如果", "虽然", "然后", "真的",
    "就是", "这个", "那个", "时候", "东西", "知道", "觉得", "可以", "已经", "现在",
    "大家", "今天", "明天", "昨天", "哈哈哈", "哈哈", "哈哈哈", "不行", "不是",
    "这个", "不能", "不会", "做", "看到", "回复", "收到", "好的", "嗯嗯", "咯", "哈",
    "the", "and", "for", "with", "that", "this", "you", "your", "are", "was",
}
CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
WORD_RE = re.compile(r"[a-zA-Z]{3,}")


def tokenize(text: str) -> list[str]:
    words: list[str] = []
    for w in CJK_RE.findall(text):
        words.append(w)
    for w in WORD_RE.findall(text):
        words.append(w.lower())
    return [w for w in words if w not in STOPWORDS and not w.isdigit()]


class HotwordStats:
    """热词统计器（复用 FtsSearcher 的读路径）"""

    def __init__(self, db_dir: str, enc_key: str | None = None, keys_file: str | None = None):
        self._fts = FtsSearcher(db_dir, enc_key, keys_file)

    def stats(self, days: int | None = None, session_id: str | None = None,
              begin_ts: int | None = None, end_ts: int | None = None,
              group_by: str = "day") -> dict:
        """统计热词。group_by: day（按天）/ session（按会话）"""
        if begin_ts is None and days:
            begin_ts = int((datetime.now() - timedelta(days=days)).timestamp())
        if end_ts is None:
            end_ts = int(datetime.now().timestamp())
        return self._stats_scan(session_id=session_id, begin_ts=begin_ts,
                                end_ts=end_ts, group_by=group_by)

    def _stats_scan(self, session_id=None, begin_ts=None, end_ts=None,
                    group_by: str = "day") -> dict:
        """直读 FTS _content 全表统计（绕开 search 的 limit 截断）"""
        dbp = self._fts.find_fts_db()
        if not dbp:
            return {"buckets": {}, "total_rows": 0, "warn": "message_fts.db not found"}
        key = self._fts._get_key(dbp)
        if not key:
            return {"buckets": {}, "total_rows": 0, "warn": "找不到 message_fts.db 的密钥"}

        from wcdb_core import WcdbSession
        fts_n2i = self._fts.load_fts_name2id()
        disp_names = self._fts.load_display_names()

        session_rowid = None
        if session_id:
            rowid, _disp = self._fts.resolve_session(session_id)
            session_rowid = rowid
            if rowid is None:
                return {"buckets": {}, "total_rows": 0, "warn": _disp}

        buckets: dict[str, Counter] = {}
        total_rows = 0
        with WcdbSession(db_path=dbp, enc_key=key) as db:
            for tbl in self._fts._content_tables(db):
                sql = (f"SELECT c0 AS acontent, c4 AS session_id, "
                       f"c6 AS create_time FROM {tbl} WHERE 1=1")
                params: list = []
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
                    print(f"  [WARN] {tbl} 统计失败: {e}", file=sys.stderr)
                    continue
                for r in rows:
                    total_rows += 1
                    content = r["acontent"] or ""
                    if group_by == "day":
                        key_bucket = datetime.fromtimestamp(r["create_time"]).strftime("%Y-%m-%d") \
                            if r["create_time"] else "unknown"
                    else:
                        s_u = fts_n2i.get(r["session_id"], "?")
                        key_bucket = disp_names.get(s_u, s_u)
                    c = buckets.setdefault(key_bucket, Counter())
                    c.update(tokenize(content))

        return {"buckets": {k: dict(v) for k, v in buckets.items()},
                "total_rows": total_rows, "warn": None,
                "begin_ts": begin_ts, "end_ts": end_ts}

    def markdown(self, result: dict, top: int = 20) -> str:
        lines = [f"# 热词统计", ""]
        if result.get("warn"):
            lines.append(f"[!] {result['warn']}")
            return "\n".join(lines)
        lines.append(f"扫描消息 {result.get('total_rows', 0)} 条\n")
        ranks = []
        for bucket, cnt in result.get("buckets", {}).items():
            for word, n in Counter(cnt).most_common(top):
                ranks.append((bucket, word, n))
        lines.append("| 时间/会话 | 热词 | 次数 |")
        lines.append("|---|---|---:|")
        for bucket, word, n in sorted(ranks, key=lambda x: (x[0], -x[2])):
            lines.append(f"| {bucket} | {word} | {n} |")
        return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="热词统计（读微信自带 FTS 索引，只读）")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex，所有库共用）")
    ap.add_argument("--keys", help="all_keys.json 路径（每个库独立密钥）")
    ap.add_argument("--session", help="限定会话：username 或 群名/昵称")
    ap.add_argument("--days", type=int, help="统计最近 N 天")
    ap.add_argument("--begin", help="起始时间 YYYY-MM-DD")
    ap.add_argument("--end", help="结束时间 YYYY-MM-DD")
    ap.add_argument("--group-by", choices=["day", "session"], default="day",
                    help="按天或按会话分桶")
    ap.add_argument("--top", type=int, default=20, help="每桶取前 N 个热词")
    ap.add_argument("--out", help="输出 Markdown 文件路径（不传则 stdout）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    begin_ts = end_ts = None
    if args.begin:
        begin_ts = int(datetime.strptime(args.begin, "%Y-%m-%d").timestamp())
    if args.end:
        end_ts = int((datetime.strptime(args.end, "%Y-%m-%d") + timedelta(days=1)).timestamp())

    stats = HotwordStats(args.db_dir, args.key, args.keys)
    result = stats.stats(days=args.days, session_id=args.session,
                         begin_ts=begin_ts, end_ts=end_ts, group_by=args.group_by)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    md = stats.markdown(result, top=args.top)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"热词报告已写入: {args.out}")
    else:
        print(md)


if __name__ == "__main__":
    main()