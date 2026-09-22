#!/usr/bin/env python3
"""sender_profile.py — 会话主理人画像（谁在说话 / 何时说话 / 说什么）

基于直连加密库逐条读消息，聚合出：
- 发送者 TOP 榜（发言数/占比）
- 活跃时段 / 星期分布
- 消息类型构成
- 首发/末发 / 峰值日

只读，不改任何库。输出 JSON 或 Markdown。

用法:
    python sender_profile.py --db-dir <db_storage> --keys <all_keys.json> \
        --session <会话username> --top 10
    python sender_profile.py --db-dir <db_storage> --keys <all_keys.json> \
        --session <会话username> --md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

from msg_reader import MessageReader, LOCAL_TYPE_LABEL
from search_fts5 import FtsSearcher


class SenderProfiler:
    """会话发送者画像"""

    def __init__(self, db_dir: str, enc_key: str | None = None,
                 keys_file: str | None = None):
        self._reader = MessageReader(db_dir, enc_key, keys_file)
        self._fts = FtsSearcher(db_dir, enc_key, keys_file)

    def profile(self, session_id: str, top: int = 10,
                begin_ts: int | None = None, end_ts: int | None = None) -> dict:
        disp = self._fts.load_display_names()
        by_user: Counter = Counter()
        user_days: dict[str, set] = defaultdict(set)
        first_ts: dict[str, int] = {}
        last_ts: dict[str, int] = {}
        hour_counter: Counter = Counter()
        weekday_counter: Counter = Counter()
        day_counter: Counter = Counter()
        type_counter: Counter = Counter()
        total = 0

        for r in self._reader.iter_messages(session_id, begin_ts, end_ts):
            total += 1
            usr = r.get("_sender_user") or "unknown"
            by_user[usr] += 1
            ts = r.get("create_time")
            if ts:
                dt = datetime.fromtimestamp(ts)
                hour_counter[dt.hour] += 1
                weekday_counter[dt.weekday()] += 1
                dkey = dt.strftime("%Y-%m-%d")
                day_counter[dkey] += 1
                user_days[usr].add(dkey)
                if ts < first_ts.get(usr, float("inf")):
                    first_ts[usr] = ts
                if ts > last_ts.get(usr, 0):
                    last_ts[usr] = ts
            type_counter[LOCAL_TYPE_LABEL.get(r.get("local_type"), "other")] += 1

        senders = []
        for usr, cnt in by_user.most_common(top):
            senders.append({
                "sender": usr,
                "display": disp.get(usr, usr),
                "count": cnt,
                "percent": round(cnt / total * 100, 1) if total else 0,
                "active_days": len(user_days[usr]),
                "first_ts": first_ts.get(usr),
                "last_ts": last_ts.get(usr),
            })

        peak_day, peak_day_n = day_counter.most_common(1)[0] if day_counter else (None, 0)
        return {
            "session": session_id,
            "total_messages": total,
            "top_senders": senders,
            "hourly": dict(sorted(hour_counter.items())),
            "weekday": [weekday_counter[i] for i in range(7)],
            "by_day": dict(day_counter.most_common(20)),
            "peak_day": {"date": peak_day, "messages": peak_day_n},
            "by_type": dict(type_counter.most_common()),
        }

    def markdown(self, p: dict, top: int = 10) -> str:
        lines = [f"# 会话画像: {p['session']}",
                 f"共 {p['total_messages']} 条消息\n",
                 "## 发送者 TOP",
                 "| 排名 | 发送者 | 条数 | 占比 | 活跃天数 |",
                 "|---|---|---:|---:|---:|"]
        for i, s in enumerate(p["top_senders"], 1):
            lines.append(f"| {i} | {s['display']} | {s['count']} | "
                         f"{s['percent']}% | {s['active_days']} |")
        lines.append("\n## 时段活跃度")
        lines.append("| 时段 | 条数 |")
        lines.append("|---|---:|")
        for h in range(24):
            n = p["hourly"].get(h, 0)
            if n:
                bar = "█" * max(1, round(n / max(p["hourly"].values()) * 20))
                lines.append(f"| {h:02d}:00 | {n} {bar} |")
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        lines.append("\n## 星期分布")
        for i, n in enumerate(p["weekday"]):
            if n:
                lines.append(f"- {weekday_names[i]}: {n}")
        if p.get("peak_day", {}).get("date"):
            lines.append(f"\n峰值日: {p['peak_day']['date']} "
                         f"({p['peak_day']['messages']} 条)")
        lines.append("\n## 消息类型")
        for t, n in p["by_type"].items():
            lines.append(f"- {t}: {n}")
        return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="会话发送者画像（只读）")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex，所有库共用）")
    ap.add_argument("--keys", help="all_keys.json 路径（每个库独立密钥）")
    ap.add_argument("--session", required=True, help="会话 username")
    ap.add_argument("--top", type=int, default=10, help="发送者 TOP N")
    ap.add_argument("--begin", help="起始时间 YYYY-MM-DD")
    ap.add_argument("--end", help="结束时间 YYYY-MM-DD")
    ap.add_argument("--md", action="store_true", help="Markdown 输出（默认 JSON）")
    args = ap.parse_args()

    begin_ts = end_ts = None
    if args.begin:
        begin_ts = int(datetime.strptime(args.begin, "%Y-%m-%d").timestamp())
    if args.end:
        end_ts = int((datetime.strptime(args.end, "%Y-%m-%d")
                      + datetime.timedelta(days=1)).timestamp())

    prof = SenderProfiler(args.db_dir, args.key, args.keys)
    result = prof.profile(args.session, top=args.top, begin_ts=begin_ts, end_ts=end_ts)
    if args.md:
        print(prof.markdown(result, top=args.top))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()