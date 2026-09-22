#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chat_realtime.py — 微信 4.x 消息实时同步【技术底层】（直连加密库，只读）

⚠️ 本模块只提供底层能力，不实现任何上层形态（CLI follow / SSE / Web 轮询 / 推送
    由使用者自行决定如何对接）。上层可选方案见文件末尾「上层接入建议」。

技术底层能力：
  1. 变更检测（WCDB data_version）
       pysqlcipher3 直连时对 message 库执行 `PRAGMA data_version`，微信每次写库
       该值 +1（SQLCipher/WCDB 语义同 SQLite）。比轮询 mtime 快且不读行。
       注意：直连只读会话在同一连接上的 data_version 会实时反映外部写入；
       若需跨连接检测，每次新开会话查询即可。
  2. 增量查询
       按 `create_time > last_ts`（秒级）拉取新消息；同秒内多条用
       `sort_seq` 与 seen(server_id) 双重去重，跨分片由 MessageReader 兜底。
  3. 持续轮询 watch()（生成器）
       内部维护 anchor，循环 poll，yield 每条新消息结构化行。

用法（数据接口）:
    from chat_realtime import RealtimeWatcher
    w = RealtimeWatcher(db_dir, keys_file="all_keys.json")
    ts = w.latest_ts("53241047527@chatroom")          # 当前最新时间戳（锚点）
    msgs, new_ts, _ = w.poll("53241047527@chatroom", since_ts=ts)   # 拉一次增量
    for m in w.watch("53241047527@chatroom", since_ts=ts, interval=5.0):
        print(m["create_time"], m.get("_sender_user"), m["local_type"])

已知边界：
    - 私聊/群聊都可用（session 传 username）。
    - 撤回/删除不通过增量查询感知（create_time 不回退）；如需感知删除，
      上层可额外比对 data_version 变化后对锚点附近做全量 diff（底层未封装）。
    - 消息库分片：message_0.db ~ message_N.db 都会扫描（MessageReader 已处理）。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msg_reader import MessageReader, msg_body  # noqa: E402
from wcdb_core import WcdbSession, find_db_files, load_keys, get_db_key_for_file  # noqa: E402


class RealtimeWatcher:
    """消息实时增量底层（只读直连）。

    anchor 语义：{"ts": int, "seen": set[int] | None}
      ts   = 上次已消费的最大 create_time
      seen = 上次 ts 时刻已消费的 server_id 集合（同秒多条去重）
    """

    def __init__(self, db_dir: str, keys_file: str | None = None,
                 enc_key: str | None = None):
        self.db_dir = db_dir
        self.reader = MessageReader(db_dir, enc_key=enc_key, keys_file=keys_file)
        self.enc_key = enc_key
        self.keys = load_keys(keys_file) if keys_file else {}

    # ---------- 变更检测 ----------
    def data_version(self, db_path: str | None = None) -> int | None:
        """PRAGMA data_version（外部写入后 +1）；失败返回 None。

        未指定 db_path 时返回第一个 message 库的值（多分片取最大值）。
        """
        dbs = [db_path] if db_path else self.reader.find_message_dbs()
        max_v = None
        for p in dbs:
            key = self.enc_key or get_db_key_for_file(p, self.db_dir, self.keys)
            if not key:
                continue
            try:
                with WcdbSession(db_path=p, enc_key=key) as db:
                    r = db.query("PRAGMA data_version")
                    if r and r[0] and r[0].get("data_version") is not None:
                        v = r[0]["data_version"]
                        max_v = v if max_v is None else max(max_v, v)
            except Exception:
                continue
        return max_v

    # ---------- 锚点 ----------
    def latest_ts(self, session: str) -> int:
        """当前最新 create_time（锚点初始值；无消息返回 0）"""
        ts = 0
        for m in self.reader.iter_messages(session_id=session, local_types=None):
            if (m.get("create_time") or 0) > ts:
                ts = m["create_time"]
        return ts

    # ---------- 增量拉取 ----------
    def poll(self, session: str, since_ts: int = 0,
             anchor_seen: set | None = None) -> tuple[list[dict], int, set]:
        """拉取 create_time >= since_ts 的增量消息。

        返回 (msgs, new_ts, seen)：
          msgs   = 新增消息行（含 _sender_user / _db_path / _table）
          new_ts = 本次最大 create_time（无新消息时等于 since_ts）
          seen   = new_ts 时刻的 server_id 集合（作为下轮 anchor_seen）

        去重规则：ts > since_ts 全收；ts == since_ts 仅收 anchor_seen 中未见过的
        server_id（同秒多条不会漏）。
        """
        seen = set(anchor_seen or set())
        msgs = []
        new_ts = since_ts
        for m in self.reader.iter_messages(session_id=session, begin_ts=since_ts):
            ts = m.get("create_time") or 0
            sid = m.get("server_id")
            if ts > since_ts:
                msgs.append(m)
                if ts > new_ts:
                    new_ts = ts
            elif ts == since_ts:
                if sid is not None and sid in seen:
                    continue
                msgs.append(m)
        # 重建 seen：仅保留 == new_ts 的行
        if new_ts > since_ts:
            seen = {m.get("server_id") for m in msgs if (m.get("create_time") or 0) == new_ts}
        elif new_ts == since_ts:
            for m in msgs:
                if (m.get("create_time") or 0) == since_ts and m.get("server_id") is not None:
                    seen.add(m["server_id"])
        return msgs, new_ts, seen

    # ---------- 持续轮询（生成器，底层形态） ----------
    def watch(self, session: str, since_ts: int | None = None,
              interval: float = 5.0, max_iters: int | None = None):
        """持续轮询，yield 每条新消息（阻塞式生成器）。

        上层可据此做：SSE 推送 / WebSocket / 命令行 follow / 事件回调。
        since_ts 省略时自动取当前最新时间戳（只收之后的新消息）。
        interval 为轮询间隔（秒）；max_iters 用于测试（None=无限）。
        """
        if since_ts is None:
            since_ts = self.latest_ts(session)
        anchor_seen: set | None = None
        iters = 0
        while max_iters is None or iters < max_iters:
            msgs, new_ts, seen = self.poll(session, since_ts=since_ts,
                                           anchor_seen=anchor_seen)
            for m in msgs:
                yield m
            since_ts = new_ts
            anchor_seen = seen
            iters += 1
            time.sleep(interval)


# 上层接入建议（不在此实现）：
#   A. 命令行 follow：`python -c "for m in RealtimeWatcher(...).watch('xxx@chatroom'): ..."`
#   B. SSE 服务（FastAPI/Flask）：watch 生成器 → EventSourceResponse 逐条推送
#   C. 业务监听：单独线程跑 watch，回调 on_message(row)；配合 data_version 感知删除
#   D. 定时快照：每 N 秒调 poll() 拉增量落库，锚点存 Redis/文件，重启续跑


def _demo():
    """最小演示：拉取一次当前最新 5 条消息（非上层形态）"""
    db_dir = r"D:\常用软件\微信缓存\xwechat_files\wxid_b8uciz1dhers22_8bb3\db_storage"
    keys = r"C:\Users\wushuo\.wxcache\all_keys.json"
    w = RealtimeWatcher(db_dir, keys_file=keys)
    ts = w.latest_ts("53241047527@chatroom")
    msgs, new_ts, seen = w.poll("53241047527@chatroom", since_ts=ts - 3600)
    print(f"anchor_ts={ts} 最近1小时增量 {len(msgs)} 条, new_ts={new_ts}")
    for m in msgs[-5:]:
        print(f"  {m.get('create_time')} {m.get('_sender_user')} type={m.get('local_type')}"
              f" {msg_body(m)[:40]!r}")


if __name__ == "__main__":
    _demo()
