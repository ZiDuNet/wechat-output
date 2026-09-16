#!/usr/bin/env python3
"""watch_messages.py - read-only incremental message watcher.

This is the local-database counterpart to a client automation listener.  It
does not touch the WeChat process or UI: it polls decrypted message shards,
discovers new ``message_N.db`` files, and delivers new rows in order.

Examples:
    python watch_messages.py --dec ./decrypted --session "群名" --state watcher.json
    python watch_messages.py --dec ./decrypted --all --since 2026-09-16 \
        --once --state watcher.json

The first run establishes a baseline at the current tail unless ``--since``
is supplied.  The persisted watermark is advanced only after the output
callback succeeds (or its retry budget is exhausted), providing at-least-once
delivery across restarts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

try:
    from chat_stats import resolve_session
    from export_group_md import (ZSTD_MAGIC, find_file, q, rich_text_summary,
                                 split_prefix, try_zstd)
except ModuleNotFoundError:
    # Allow ``import scripts.watch_messages`` from the repository root while
    # preserving the direct-script import style used by the other tools.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from chat_stats import resolve_session
    from export_group_md import (ZSTD_MAGIC, find_file, q, rich_text_summary,
                                 split_prefix, try_zstd)

MESSAGE_DB_RE = re.compile(r"^message_\d+\.db$")
TABLE_RE = re.compile(r"^Msg_[0-9a-fA-F]{32}$")
SYSTEM_TYPES = {10000, 10002, 266287972401}
TYPE_LABELS = {
    1: "文本", 3: "图片", 34: "语音", 43: "视频", 47: "表情",
    48: "位置", 49: "链接/文件", 50: "通话", 10000: "系统",
    10002: "撤回", 266287972401: "拍一拍", 244813135921: "引用",
}


def _table_ident(name: str) -> str:
    """Quote an identifier returned by sqlite_master."""
    return '"' + name.replace('"', '""') + '"'


def _type_label(local_type: int) -> str:
    if local_type in TYPE_LABELS:
        return TYPE_LABELS[local_type]
    low = (local_type or 0) & 0xFF
    return TYPE_LABELS.get(low, f"其他({local_type})")


def _parse_since(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"[x] --since 不是 Unix 秒或 ISO 日期: {value}") from exc
    if dt.tzinfo:
        return dt.timestamp()
    return time.mktime(dt.timetuple())


def _read_contact_maps(dec: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    contact_db = find_file(dec, "contact.db")
    if not contact_db:
        raise SystemExit("[x] 找不到 contact.db")
    nick: Dict[str, str] = {}
    hash2user: Dict[str, str] = {}
    for row in q(contact_db, "SELECT username, nick_name, remark FROM contact"):
        username = row["username"] or ""
        if not username:
            continue
        nick[username] = row["remark"] or row["nick_name"] or username
        hash2user[hashlib.md5(username.encode()).hexdigest()] = username
    # These accounts can have message tables without a contact row.
    for username in ("filehelper", "fmessage", "weixin", "mphelper", "medianote"):
        hash2user.setdefault(hashlib.md5(username.encode()).hexdigest(), username)
        nick.setdefault(username, username)
    return nick, hash2user


def _display_content(content, local_type: int, with_zstd: bool,
                     known_users: Set[str], max_text: int) -> Tuple[Optional[str], str]:
    sender = None
    if isinstance(content, bytes) and content.startswith(ZSTD_MAGIC):
        if with_zstd:
            content = try_zstd(content) or f"[{_type_label(local_type)}·压缩未解]"
        else:
            content = f"[{_type_label(local_type)}·压缩未解]"
    elif isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    elif content is None:
        content = ""
    else:
        content = str(content)

    content = content.replace("\r\n", "\n").replace("\r", "")
    if content:
        sender, content = split_prefix(content, known_users)
    # Keep output compact and consistent with the existing exporters.
    if content.lstrip().startswith("<"):
        title, des = rich_text_summary(content)
        if title or des:
            content = f"[{_type_label(local_type)}] " + " | ".join(
                value for value in (title, des) if value
            )
        elif local_type in (34, 43):
            content = f"[{_type_label(local_type)}]"
    content = content.strip()
    if max_text and len(content) > max_text:
        content = content[:max_text] + "…"
    return sender, content


class MessageWatcher:
    """Poll decrypted message shards and deliver unseen messages.

    ``callback`` receives one JSON-serializable dictionary.  It should raise
    on failed processing; failed callbacks are retried and the row is then
    acknowledged so a bad consumer cannot block all later messages forever.
    """

    def __init__(self, dec: str, usernames: Optional[Iterable[str]] = None,
                 all_sessions: bool = False, state_file: Optional[str] = None,
                 since: Optional[float] = None, with_zstd: bool = True,
                 max_text: int = 300, max_retries: int = 3,
                 retry_delay: float = 1.0):
        self.dec = os.path.abspath(dec)
        self.nick, self.hash2user = _read_contact_maps(self.dec)
        self.usernames = set(usernames or ())
        self.all_sessions = all_sessions
        self.state_file = os.path.abspath(state_file) if state_file else ""
        self.since = since
        self.with_zstd = with_zstd
        self.max_text = max(0, int(max_text))
        self.max_retries = max(0, int(max_retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self._state: Dict[str, List[int]] = {}
        self._initialized: Set[str] = set()
        self._lock = threading.Lock()
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError):
            return
        raw = data.get("watermarks", data) if isinstance(data, dict) else {}
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if isinstance(value, (list, tuple)) and len(value) == 3:
                try:
                    self._state[key] = [int(value[0]), int(value[1]), int(value[2])]
                except (TypeError, ValueError):
                    continue

    def _save_state(self) -> None:
        if not self.state_file:
            return
        payload = {"version": 1, "watermarks": self._state}
        directory = os.path.dirname(self.state_file) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            print(f"[!] 水位保存失败: {exc}", file=sys.stderr)

    def _message_dbs(self) -> List[str]:
        paths: List[str] = []
        for root, _dirs, files in os.walk(self.dec):
            for name in files:
                if MESSAGE_DB_RE.match(name):
                    paths.append(os.path.join(root, name))
        return sorted(paths)

    def _selected(self, username: Optional[str]) -> bool:
        return self.all_sessions or username in self.usernames or username is None

    @staticmethod
    def _key(rel_db: str, table: str) -> str:
        return rel_db.replace(os.sep, "/") + ":" + table

    @staticmethod
    def _row_key(row) -> Tuple[int, int, int]:
        return (int(row["create_time"] or 0), int(row["sort_seq"] or 0),
                int(row["local_id"] or 0))

    def _tables(self, db: str):
        import sqlite3
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = [row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")
                      if TABLE_RE.match(row["name"])]
            try:
                n2i = {int(row["rid"]): row["user_name"] for row in conn.execute(
                    "SELECT rowid AS rid, user_name FROM Name2Id")}
            except sqlite3.DatabaseError:
                n2i = {}
            return conn, tables, n2i
        except Exception:
            conn.close()
            raise

    def _session_watermark(self, username: Optional[str]) -> Optional[Tuple[int, int, int]]:
        """Return the highest acknowledged watermark for a session's other shards."""
        if not username:
            return None
        expected = hashlib.md5(username.encode()).hexdigest().lower()
        values = []
        for key, value in self._state.items():
            table = key.rsplit(":", 1)[-1]
            if table.startswith("Msg_") and table[4:].lower() == expected:
                values.append(tuple(value))
        return max(values) if values else None

    def _baseline(self, conn, table: str, state_key: str,
                  username: Optional[str]) -> None:
        if state_key in self._state or state_key in self._initialized:
            return
        prior = self._session_watermark(username)
        if prior is not None:
            self._state[state_key] = list(prior)
        elif self.since is not None:
            self._state[state_key] = [int(self.since) - 1, -1, -1]
        else:
            row = conn.execute(
                f"SELECT local_id, create_time, sort_seq FROM {_table_ident(table)} "
                "ORDER BY create_time DESC, sort_seq DESC, local_id DESC LIMIT 1"
            ).fetchone()
            self._state[state_key] = list(self._row_key(row)) if row else [0, -1, -1]
        self._initialized.add(state_key)

    def _event(self, row, username: Optional[str], display_name: str,
               n2i: Dict[int, str], known_users: Set[str], state_key: str) -> dict:
        local_type = int(row["local_type"] or 0)
        sender_u, content = _display_content(
            row["message_content"], local_type, self.with_zstd, known_users, self.max_text
        )
        sender_u = sender_u or n2i.get(int(row["real_sender_id"] or 0)) or ""
        sender_display = self.nick.get(sender_u, sender_u) if sender_u else "我"
        ts = int(row["create_time"] or 0)
        event_id = hashlib.sha256(
            f"{state_key}:{row['local_id']}:{ts}:{row['sort_seq']}".encode()
        ).hexdigest()[:24]
        return {
            "event_id": event_id,
            "username": username or f"<未知会话:{state_key.rsplit('Msg_', 1)[-1]}>",
            "session": display_name,
            "create_time": ts,
            "time": datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="seconds"),
            "local_id": int(row["local_id"] or 0),
            "sort_seq": int(row["sort_seq"] or 0),
            "sender": sender_u,
            "sender_display": sender_display,
            "local_type": local_type,
            "type": _type_label(local_type),
            "is_system": local_type in SYSTEM_TYPES,
            "content": content,
        }

    def _iter_unseen(self):
        known_users = set(self.nick) | set(self.hash2user.values())
        for db in self._message_dbs():
            rel_db = os.path.relpath(db, self.dec)
            try:
                conn, tables, n2i = self._tables(db)
            except Exception as exc:
                print(f"[!] 读取 {rel_db} 失败: {exc}", file=sys.stderr)
                continue
            try:
                known_users.update(n2i.values())
                for table in tables:
                    username = self.hash2user.get(table[4:].lower())
                    if not self._selected(username):
                        continue
                    if username is None and not self.all_sessions:
                        continue
                    state_key = self._key(rel_db, table)
                    self._baseline(conn, table, state_key, username)
                    watermark = tuple(self._state[state_key])
                    rowset = conn.execute(
                        f"SELECT local_id, local_type, real_sender_id, create_time, "
                        f"sort_seq, message_content FROM {_table_ident(table)} "
                        "WHERE create_time >= ? ORDER BY create_time, sort_seq, local_id",
                        (watermark[0],),
                    ).fetchall()
                    display_name = self.nick.get(username, username or state_key)
                    for row in rowset:
                        if self._row_key(row) <= watermark:
                            continue
                        yield state_key, self._row_key(row), self._event(
                            row, username, display_name, n2i, known_users, state_key
                        )
            finally:
                conn.close()

    def poll(self) -> List[Tuple[str, Tuple[int, int, int], dict]]:
        """Return unseen rows sorted globally without acknowledging them."""
        rows = list(self._iter_unseen())
        rows.sort(key=lambda item: (item[1], item[0]))
        return rows

    def acknowledge(self, state_key: str, row_key: Tuple[int, int, int]) -> None:
        with self._lock:
            old = tuple(self._state.get(state_key, [0, -1, -1]))
            if row_key > old:
                self._state[state_key] = list(row_key)
                self._save_state()

    def run_once(self, callback: Callable[[dict], None]) -> int:
        delivered = 0
        for state_key, row_key, event in self.poll():
            ok = False
            for attempt in range(self.max_retries + 1):
                try:
                    callback(event)
                    ok = True
                    break
                except Exception as exc:
                    if attempt >= self.max_retries:
                        print(
                            f"[!] 回调失败，已丢弃 event_id={event['event_id']}: {exc}",
                            file=sys.stderr,
                        )
                    else:
                        time.sleep(self.retry_delay)
            # Acknowledging after retry exhaustion mirrors the upstream
            # listener: one bad consumer cannot permanently block the queue.
            self.acknowledge(state_key, row_key)
            delivered += 1
        return delivered


def _resolve_users(dec: str, sessions: List[str]) -> Set[str]:
    users: Set[str] = set()
    for value in sessions:
        username, _display, _is_group = resolve_session(dec, value)
        users.add(username)
    return users


def main() -> None:
    ap = argparse.ArgumentParser(description="监听解密后的微信消息（只读、跨分片）")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--session", action="append", default=[],
                    help="群名/联系人名或 username；可重复指定")
    ap.add_argument("--all", dest="all_sessions", action="store_true",
                    help="监听所有已知会话，并自动发现新分片")
    ap.add_argument("--state", help="JSON 水位文件；不指定则不跨重启保存")
    ap.add_argument("--since", help="首次启动回放起点：Unix 秒或 ISO 日期时间")
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒数（默认 1）")
    ap.add_argument("--once", action="store_true", help="只轮询一轮后退出")
    ap.add_argument("--format", choices=("jsonl", "text"), default="jsonl",
                    help="输出格式（默认 jsonl）")
    ap.add_argument("--out", help="输出文件；不指定则输出到 stdout")
    ap.add_argument("--max-text", type=int, default=300,
                    help="单条正文截断字数（默认 300，0=不截断）")
    ap.add_argument("--no-zstd", action="store_true", help="跳过 zstd 解压")
    ap.add_argument("--max-retries", type=int, default=3,
                    help="输出失败重试次数（默认 3）")
    ap.add_argument("--retry-delay", type=float, default=1.0,
                    help="重试间隔秒数（默认 1）")
    args = ap.parse_args()
    if not args.all_sessions and not args.session:
        ap.error("需要 --session，或使用 --all 监听全部会话")
    if args.interval < 0 or args.max_retries < 0:
        ap.error("--interval 和 --max-retries 不能为负数")
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        stream = open(args.out, "a", encoding="utf-8")
    else:
        stream = sys.stdout
    try:
        watcher = MessageWatcher(
            args.dec,
            usernames=_resolve_users(args.dec, args.session),
            all_sessions=args.all_sessions,
            state_file=args.state,
            since=_parse_since(args.since),
            with_zstd=not args.no_zstd,
            max_text=args.max_text,
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
        )

        def emit(event: dict) -> None:
            if args.format == "jsonl":
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            else:
                who = "系统" if event["is_system"] else event["sender_display"]
                stream.write(
                    f"[{event['time']}] {event['session']} | {who}: {event['content']}\n"
                )
            stream.flush()

        while True:
            watcher.run_once(emit)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if args.out:
            stream.close()


if __name__ == "__main__":
    main()
