#!/usr/bin/env python3
"""contacts.py — 联系人/群组查询模块

统一封装联系人和群组的查询操作。

用法:
    from contacts import ContactManager
    with ContactManager(db_dir, enc_key) as cm:
        contact = cm.get_contact("wxid_xxx")
        members = cm.get_group_members("xxx@chatroom")
        names = cm.get_display_names(["wxid_a", "wxid_b"])
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from wcdb_core import WcdbSession, load_keys, get_db_key_for_file


class ContactManager:
    """联系人/群组管理器"""

    def __init__(self, db_dir: str, enc_key: str | None = None, keys_file: str | None = None):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}
        self._contact_db = self._find_contact_db()

    def _get_key(self, db_path: str) -> str | None:
        if self._enc_key:
            return self._enc_key
        return get_db_key_for_file(db_path, self._db_dir, self._keys)

    def _find_contact_db(self) -> str | None:
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if name == "contact.db" and not name.endswith(("-wal", "-shm")):
                    return os.path.join(root, name)
        return None

    def _open_contact(self) -> WcdbSession | None:
        if not self._contact_db:
            return None
        key = self._get_key(self._contact_db)
        if key:
            return WcdbSession(db_path=self._contact_db, enc_key=key)
        return None

    def get_contact(self, username: str) -> dict | None:
        """获取单个联系人详情"""
        db = self._open_contact()
        if not db:
            return None
        with db:
            rows = db.query(
                "SELECT username, nick_name, remark, alias, small_head_url, "
                "label_name, extra_buffer, flag, local_type "
                "FROM contact WHERE username = ?",
                (username,)
            )
            return rows[0] if rows else None

    def get_contacts_compact(self, usernames: list[str]) -> list[dict]:
        """批量获取联系人（紧凑信息）"""
        if not usernames:
            return []
        db = self._open_contact()
        if not db:
            return []
        with db:
            placeholders = ",".join("?" * len(usernames))
            return db.query(
                f"SELECT username, nick_name, remark, alias FROM contact "
                f"WHERE username IN ({placeholders})",
                tuple(usernames)
            )

    def get_display_names(self, usernames: list[str]) -> dict[str, str]:
        """获取显示名称映射 {username: display_name}"""
        contacts = self.get_contacts_compact(usernames)
        result = {}
        for c in usernames:
            match = next((x for x in contacts if x["username"] == c), None)
            if match:
                result[c] = match.get("remark") or match.get("nick_name") or c
            else:
                result[c] = c
        return result

    def get_avatar_urls(self, usernames: list[str]) -> dict[str, str]:
        """获取头像 URL 映射"""
        if not usernames:
            return {}
        db = self._open_contact()
        if not db:
            return {}
        with db:
            placeholders = ",".join("?" * len(usernames))
            rows = db.query(
                f"SELECT username, small_head_url FROM contact "
                f"WHERE username IN ({placeholders}) AND small_head_url IS NOT NULL",
                tuple(usernames)
            )
            return {r["username"]: r["small_head_url"] for r in rows}

    def get_contact_status(self, usernames: list[str]) -> list[dict]:
        """获取联系人状态"""
        if not usernames:
            return []
        db = self._open_contact()
        if not db:
            return []
        with db:
            placeholders = ",".join("?" * len(usernames))
            return db.query(
                f"SELECT username, flag, local_type FROM contact "
                f"WHERE username IN ({placeholders})",
                tuple(usernames)
            )

    def get_contact_type_counts(self) -> dict:
        """获取联系人类型统计"""
        db = self._open_contact()
        if not db:
            return {}
        with db:
            rows = db.query("""
                SELECT
                    SUM(CASE WHEN username LIKE '%@chatroom' THEN 1 ELSE 0 END) AS group_count,
                    SUM(CASE WHEN username LIKE 'gh_%' THEN 1 ELSE 0 END) AS official_count,
                    SUM(CASE WHEN username NOT LIKE '%@chatroom' AND username NOT LIKE 'gh_%'
                        AND COALESCE(flag, 0) & 8 = 0 THEN 1 ELSE 0 END) AS private_count
                FROM contact WHERE username IS NOT NULL AND username != ''
            """)
            return rows[0] if rows else {}

    def get_friend_flags(self, usernames: list[str]) -> list[dict]:
        """获取好友标记"""
        if not usernames:
            return []
        db = self._open_contact()
        if not db:
            return []
        with db:
            placeholders = ",".join("?" * len(usernames))
            return db.query(
                f"SELECT username, flag, extra_buffer FROM contact "
                f"WHERE username IN ({placeholders})",
                tuple(usernames)
            )

    def search_contacts(self, keyword: str, limit: int = 20) -> list[dict]:
        """搜索联系人（按昵称/备注模糊匹配）"""
        db = self._open_contact()
        if not db:
            return []
        with db:
            pat = f"%{keyword}%"
            return db.query(
                "SELECT username, nick_name, remark, alias FROM contact "
                "WHERE (nick_name LIKE ? OR remark LIKE ? OR alias LIKE ?) "
                "AND username IS NOT NULL LIMIT ?",
                (pat, pat, pat, limit)
            )


class GroupManager:
    """群组管理器"""

    def __init__(self, db_dir: str, enc_key: str | None = None, keys_file: str | None = None):
        self._db_dir = db_dir
        self._enc_key = enc_key
        self._keys = load_keys(keys_file) if keys_file else {}

    def _get_key(self, db_path: str) -> str | None:
        if self._enc_key:
            return self._enc_key
        return get_db_key_for_file(db_path, self._db_dir, self._keys)

    def _find_contact_db(self) -> str | None:
        for root, _dirs, files in os.walk(self._db_dir):
            for name in files:
                if name == "contact.db" and not name.endswith(("-wal", "-shm")):
                    return os.path.join(root, name)
        return None

    def get_group_members(self, chatroom_id: str) -> list[dict]:
        """获取群成员列表"""
        contact_db = self._find_contact_db()
        if not contact_db:
            return []
        key = self._get_key(contact_db)
        if not key:
            return []
        with WcdbSession(db_path=contact_db, enc_key=key) as db:
            return db.query(
                "SELECT n.rowid, n.username, c.member_id "
                "FROM name2id n "
                "LEFT JOIN chatroom_member c ON n.rowid = c.room_id "
                "WHERE n.username = ?",
                (chatroom_id,)
            )

    def get_group_member_count(self, chatroom_id: str) -> int:
        """获取群成员数"""
        members = self.get_group_members(chatroom_id)
        return len(members)

    def get_group_nicknames(self, chatroom_id: str) -> dict[str, str]:
        """获取群内成员昵称映射"""
        contact_db = self._find_contact_db()
        if not contact_db:
            return {}
        key = self._get_key(contact_db)
        if not key:
            return {}
        with WcdbSession(db_path=contact_db, enc_key=key) as db:
            rooms = db.query(
                "SELECT rowid FROM name2id WHERE username = ?",
                (chatroom_id,)
            )
            if not rooms:
                return {}
            room_id = rooms[0]["rowid"]
            rows = db.query(
                "SELECT n.username, c.display_name "
                "FROM chatroom_member c "
                "JOIN name2id n ON c.member_id = n.rowid "
                "WHERE c.room_id = ?",
                (room_id,)
            )
            return {r["username"]: r["display_name"] or r["username"] for r in rows}

    def list_groups(self, keyword: str | None = None, limit: int = 50) -> list[dict]:
        """列出群聊"""
        contact_db = self._find_contact_db()
        if not contact_db:
            return []
        key = self._get_key(contact_db)
        if not key:
            return []
        with WcdbSession(db_path=contact_db, enc_key=key) as db:
            if keyword:
                pat = f"%{keyword}%"
                return db.query(
                    "SELECT username, nick_name, remark FROM contact "
                    "WHERE username LIKE '%@chatroom' "
                    "AND (nick_name LIKE ? OR remark LIKE ?) LIMIT ?",
                    (pat, pat, limit)
                )
            else:
                return db.query(
                    "SELECT username, nick_name, remark FROM contact "
                    "WHERE username LIKE '%@chatroom' LIMIT ?",
                    (limit,)
                )


def main():
    ap = argparse.ArgumentParser(description="联系人/群组查询")
    sub = ap.add_subparsers(dest="cmd")

    # contact 子命令
    c_p = sub.add_parser("contact", help="查询联系人")
    c_p.add_argument("username", help="联系人 username")
    c_p.add_argument("--db-dir", required=True)
    c_p.add_argument("--key")
    c_p.add_argument("--keys")

    # search 子命令
    s_p = sub.add_parser("search", help="搜索联系人")
    s_p.add_argument("keyword", help="搜索关键词")
    s_p.add_argument("--db-dir", required=True)
    s_p.add_argument("--key")
    s_p.add_argument("--keys")
    s_p.add_argument("--limit", type=int, default=20)

    # members 子命令
    m_p = sub.add_parser("members", help="查询群成员")
    m_p.add_argument("chatroom_id", help="群 ID")
    m_p.add_argument("--db-dir", required=True)
    m_p.add_argument("--key")
    m_p.add_argument("--keys")

    # groups 子命令
    g_p = sub.add_parser("groups", help="列出群聊")
    g_p.add_argument("--db-dir", required=True)
    g_p.add_argument("--key")
    g_p.add_argument("--keys")
    g_p.add_argument("--keyword", help="搜索关键词")
    g_p.add_argument("--limit", type=int, default=50)

    # stats 子命令
    st_p = sub.add_parser("stats", help="联系人类型统计")
    st_p.add_argument("--db-dir", required=True)
    st_p.add_argument("--key")
    st_p.add_argument("--keys")

    args = ap.parse_args()

    if args.cmd == "contact":
        cm = ContactManager(args.db_dir, args.key, getattr(args, 'keys', None))
        c = cm.get_contact(args.username)
        print(json.dumps(c, ensure_ascii=False, indent=2) if c else "未找到")

    elif args.cmd == "search":
        cm = ContactManager(args.db_dir, args.key, getattr(args, 'keys', None))
        results = cm.search_contacts(args.keyword, args.limit)
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.cmd == "members":
        gm = GroupManager(args.db_dir, args.key, getattr(args, 'keys', None))
        members = gm.get_group_members(args.chatroom_id)
        print(json.dumps(members, ensure_ascii=False, indent=2))

    elif args.cmd == "groups":
        gm = GroupManager(args.db_dir, args.key, getattr(args, 'keys', None))
        groups = gm.list_groups(args.keyword, args.limit)
        print(json.dumps(groups, ensure_ascii=False, indent=2))

    elif args.cmd == "stats":
        cm = ContactManager(args.db_dir, args.key, getattr(args, 'keys', None))
        stats = cm.get_contact_type_counts()
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    else:
        ap.print_help()


if __name__ == "__main__":
    main()
