#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sns_export.py — 微信 4.x 朋友圈导出（直连加密库，高仿微信 UI HTML）

数据源（直连，只读）：
    sns/sns.db → SnsTimeLine（朋友圈正文，content 为 XML，时间在 createTime 节点）
               → SnsMessage_tmp3（评论/点赞，feed_id ↔ SnsTimeLine.tid 关联）
    contact/contact.db → username → 昵称/备注 映射

说明：
    - 媒体取 content XML 的 mediaList url（微信 CDN 长期有效）；本地缓存解密未做，
      但解析逻辑已保留 media kind/thumb/duration。
    - 点赞 type=1，评论 type=2（SnsMessage_tmp3.type）。
    - 输出单文件 HTML，高仿微信朋友圈 UI：深色头图 + 卡片时间线 + 九宫格 + 点赞/评论。

用法：
    # 数据接口（推荐，JSON）
    from sns_export import SnsExporter
    ex = SnsExporter(db_dir, keys_file="all_keys.json")     # 或 enc_key="64hex"
    posts = ex.fetch(since_ts=..., until_ts=..., limit=50)  # list[dict]
    # 每项: {time, name, desc, location, media:[{kind,url,thumb,dur}],
    #        share_title, share_url, comments:[{type,from,content}]}

    # CLI 数据输出（默认 JSON 到 stdout；--out 写文件）
    python sns_export.py --db-dir <db_storage> --keys <all_keys.json>
    python sns_export.py --db-dir ... --keys ... --last 3m --limit 100
    # CLI 渲染（可选）
    python sns_export.py --db-dir ... --keys ... --out 朋友圈.html --html
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from xml.etree import ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wcdb_core import WcdbSession, find_db_files, load_keys, get_db_key_for_file  # noqa: E402
from media_common import add_time_args, parse_time_range  # noqa: E402


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _txt(node, path, default=""):
    if node is None:
        return default
    el = node.find(path)
    if el is None or el.text is None:
        return default
    return el.text.strip()


def parse_sns_content(content_xml: str) -> dict:
    """解析 SnsTimeLine.content XML → 结构化字段"""
    result = {"time": 0, "desc": "", "nickname": "", "location": "",
              "media": [], "share_title": "", "share_url": ""}
    if not content_xml:
        return result
    try:
        root = ET.fromstring(content_xml)
    except Exception:
        return result
    tlo = root.find("TimelineObject")
    if tlo is None:
        return result
    t = _txt(tlo, "createTime")
    if t.isdigit():
        result["time"] = int(t)
    result["desc"] = _txt(tlo, "contentDesc")
    loc = tlo.find("location")
    if loc is not None:
        result["location"] = (loc.get("poiName") or "").strip()
    co = tlo.find("ContentObject")
    if co is not None:
        result["share_title"] = _txt(co, "title")
        result["share_url"] = _txt(co, "contentUrl")
        ml = co.find("mediaList")
        if ml is not None:
            for media in ml.findall("media"):
                mtype = _txt(media, "type")
                url = _txt(media, "url")
                thumb = _txt(media, "thumb")
                dur = _txt(media, "videoDuration")
                if url:
                    result["media"].append({
                        "kind": "视频" if mtype == "6" else "图片",
                        "url": url, "thumb": thumb, "dur": dur,
                    })
    lei = root.find("LocalExtraInfo")
    if lei is not None:
        result["nickname"] = _txt(lei, "nickname") or result["nickname"]
    return result


def fmt_time(ts: int) -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


def avatar_html(name: str, size: int = 44) -> str:
    """首字渐变圆形头像（无本地头像文件，按微信风格渲染）"""
    colors = ["#7c3aed", "#2563eb", "#db2777", "#ea580c", "#16a34a",
              "#0891b2", "#4f46e5", "#be185d"]
    ch = (name or "?")[:1]
    idx = (sum(ord(c) for c in name) % len(colors)) if name else 0
    return (f'<span class="avatar" style="width:{size}px;height:{size}px;'
            f'background:{colors[idx % len(colors)]};font-size:{size * 0.42}px">'
            f'{_esc(ch)}</span>')


def render_media(media: list[dict]) -> str:
    if not media:
        return ""
    n = len(media)
    cls = "grid1" if n == 1 else ("grid2" if n == 2 else "grid3")
    cells = []
    for m in media:
        if m["kind"] == "图片":
            cells.append(f'<div class="mcell"><img loading="lazy" src="{_esc(m["url"])}" '
                         f'alt="图片" onerror="this.style.display=\'none\'"></div>')
        else:
            cells.append(f'<div class="mcell vid"><img loading="lazy" src="{_esc(m["thumb"] or m["url"])}" '
                         f'alt="视频" onerror="this.style.display=\'none\'">'
                         f'<span class="play">▶</span>{"<i>" + _esc(m["dur"]) + "</i>" if m["dur"] else ""}</div>')
    return f'<div class="media {cls}">{"".join(cells)}</div>'


def render_feed(post: dict, comments: list[dict]) -> str:
    likes = [c for c in comments if c.get("type") == 1]
    cmts = [c for c in comments if c.get("type") == 2]
    parts = []
    parts.append(f'<div class="feed"><div class="feed-hd">{avatar_html(post["name"])}'
                 f'<div class="feed-id"><div class="feed-name">{_esc(post["name"])}</div>'
                 f'<div class="feed-time">{fmt_time(post["time"])}</div></div></div>')
    if post["desc"]:
        parts.append(f'<div class="feed-desc">{_esc(post["desc"])}</div>')
    if post["location"]:
        parts.append(f'<div class="feed-loc">📍 {_esc(post["location"])}</div>')
    parts.append(render_media(post["media"]))
    if post["share_title"]:
        parts.append(f'<a class="feed-share" href="{_esc(post["share_url"])}" '
                     f'target="_blank" rel="noopener">🔗 {_esc(post["share_title"])}</a>')
    react = ""
    if likes:
        names = "、".join(_esc(c.get("from") or "朋友") for c in likes)
        react += f'<div class="feed-like">👍 {names}</div>'
    if cmts:
        rows = "".join(
            f'<div class="feed-cmt"><b>{_esc(c.get("from") or "朋友")}：</b>'
            f'{_esc(re.sub(r"<[^>]+>", "", c.get("content") or ""))}</div>'
            for c in cmts)
        react += f'<div class="feed-cmts">{rows}</div>'
    if react:
        parts.append(f'<div class="feed-react">{react}</div>')
    parts.append("</div>")
    return "".join(parts)


def load_contacts(db_dir: str, keys: dict) -> dict:
    names = {}
    for root, _d, files in os.walk(db_dir):
        for name in files:
            if name.startswith("contact") and name.endswith(".db") \
                    and not name.endswith(("-wal", "-shm")) and "fts" not in name:
                path = os.path.join(root, name)
                key = get_db_key_for_file(path, db_dir, keys)
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
    return names


class SnsExporter:
    """朋友圈数据接口（直连加密库，只读）。

    from sns_export import SnsExporter
    ex = SnsExporter(db_dir, keys_file="all_keys.json")
    posts = ex.fetch(since_ts=None, until_ts=None, limit=0)   # 按 createTime 过滤
    """

    def __init__(self, db_dir: str, keys_file: str | None = None,
                 enc_key: str | None = None):
        self.db_dir = db_dir
        self.keys = load_keys(keys_file) if keys_file else {}
        self.enc_key = enc_key
        self._contacts = None

    def _sns_db(self) -> tuple[str, str]:
        cats = find_db_files(self.db_dir)
        if not cats.get("sns"):
            raise FileNotFoundError("未找到 sns 库（sns/sns.db）")
        sns_db = cats["sns"][0]
        key = self.enc_key or get_db_key_for_file(sns_db, self.db_dir, self.keys)
        if not key:
            raise RuntimeError("无 sns 库密钥，请提供 --keys / --key")
        return sns_db, key

    def _contact_names(self) -> dict:
        if self._contacts is not None:
            return self._contacts
        self._contacts = load_contacts(self.db_dir, self.keys)
        return self._contacts

    def fetch(self, since_ts: int | None = None, until_ts: int | None = None,
              limit: int = 0) -> list[dict]:
        """返回朋友圈结构化数据（按时间倒序）。

        每项字段：time(时间戳) name(作者) desc(正文) location(位置)
                  media([{kind,url,thumb,dur}]) share_title/share_url
                  comments([{type:1赞/2评, from, content}])
        """
        sns_db, key = self._sns_db()
        names = self._contact_names()
        comments: dict[int, list[dict]] = {}
        try:
            with WcdbSession(db_path=sns_db, enc_key=key) as db:
                for r in db.query("SELECT feed_id, type, from_nickname, content "
                                  "FROM SnsMessage_tmp3"):
                    fid = r.get("feed_id")
                    if fid is None:
                        continue
                    comments.setdefault(fid, []).append({
                        "type": r.get("type"), "from": (r.get("from_nickname") or "").strip(),
                        "content": r.get("content") or "",
                    })
        except Exception as e:
            print(f"  [WARN] 评论读取失败: {e}", file=sys.stderr)

        posts = []
        with WcdbSession(db_path=sns_db, enc_key=key) as db:
            for r in db.query("SELECT tid, user_name, content FROM SnsTimeLine ORDER BY tid"):
                info = parse_sns_content(r.get("content") or "")
                ts = info["time"]
                if since_ts and ts < since_ts:
                    continue
                if until_ts and ts > until_ts:
                    continue
                uid = (r.get("user_name") or "").strip()
                name = info["nickname"] or names.get(uid) or uid or "我"
                posts.append({
                    "time": ts, "name": name, "desc": info["desc"],
                    "location": info["location"], "media": info["media"],
                    "share_title": info["share_title"], "share_url": info["share_url"],
                    "comments": comments.get(r.get("tid"), []),
                })
        posts.sort(key=lambda p: -p["time"])
        if limit:
            posts = posts[:limit]
        return posts


def main():
    ap = argparse.ArgumentParser(description="微信朋友圈数据接口（直连加密库）")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--keys", help="all_keys.json 路径")
    ap.add_argument("--key", help="64hex 统一密钥（无 keys 文件时）")
    ap.add_argument("--out", help="输出文件（默认 stdout JSON）")
    add_time_args(ap)
    ap.add_argument("--limit", type=int, default=0, help="最多返回条数（0=全部）")
    ap.add_argument("--html", action="store_true", help="输出高仿微信 UI HTML（可选）")
    args = ap.parse_args()

    begin_ts, end_ts = parse_time_range(args.since, args.until, args.last)
    ex = SnsExporter(args.db_dir, keys_file=args.keys, enc_key=args.key)
    posts = ex.fetch(since_ts=begin_ts, until_ts=end_ts, limit=args.limit)

    if not args.html:
        out = args.out or "-"
        payload = {"total": len(posts), "posts": posts}
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        if out == "-":
            print(text)
        else:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
        print(f"朋友圈数据: {len(posts)} 条 -> {out}", file=sys.stderr)
        return

    total = len(posts)
    img_cnt = sum(len(p["media"]) for p in posts)
    like_cnt = sum(1 for p in posts for c in p["comments"] if c.get("type") == 1)
    cmt_cnt = sum(1 for p in posts for c in p["comments"] if c.get("type") == 2)
    author_cnt = len({p["name"] for p in posts})

    feeds = "".join(render_feed(p, p["comments"]) for p in posts)
    empty = "" if posts else ('<div class="empty">该时间范围内没有朋友圈记录。</div>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>朋友圈 · 时光轴</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{background:#ededed;color:#191919;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.55;}}
  .banner{{background:linear-gradient(180deg,#1c2333,#2c3e50 60%,#3a4a5c);color:#fff;text-align:center;padding:30px 16px 54px;position:relative;}}
  .banner h1{{font-size:22px;font-weight:700;letter-spacing:.12em;}}
  .banner .sub{{font-size:12px;color:#aab8c8;margin-top:6px;}}
  .banner .mask{{position:absolute;left:0;right:0;bottom:-1px;height:26px;background:#ededed;border-radius:26px 26px 0 0;}}
  .wrap{{max-width:640px;margin:-32px auto 0;padding:0 12px 50px;position:relative;z-index:2;}}
  .mine{{display:flex;align-items:center;gap:12px;justify-content:flex-end;padding:14px 16px;margin-bottom:8px;}}
  .mine .my-name{{font-size:15px;font-weight:700;color:#111;}}
  .feed{{background:#fff;border-radius:14px;padding:14px 14px 6px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.06);}}
  .feed-hd{{display:flex;align-items:center;gap:10px;margin-bottom:8px;}}
  .feed-id{{flex:1;min-width:0;}}
  .feed-name{{font-size:14px;font-weight:600;color:#111;}}
  .feed-time{{font-size:11px;color:#8a8a8a;margin-top:1px;}}
  .feed-desc{{font-size:14px;color:#191919;margin:2px 0 8px;white-space:pre-wrap;word-break:break-word;}}
  .feed-loc{{font-size:12px;color:#576b95;margin-bottom:6px;}}
  .feed-share{{display:block;font-size:13px;color:#576b95;background:#f7f7f7;border-radius:8px;padding:9px 12px;margin:8px 0;text-decoration:none;word-break:break-all;}}
  .media{{display:grid;gap:4px;margin:8px 0;}}
  .media.grid1{{grid-template-columns:minmax(0,260px);}}
  .media.grid2{{grid-template-columns:repeat(2,1fr);}}
  .media.grid3{{grid-template-columns:repeat(3,1fr);}}
  .mcell{{position:relative;aspect-ratio:1;overflow:hidden;border-radius:6px;background:#f0f0f0;}}
  .mcell img{{width:100%;height:100%;object-fit:cover;display:block;}}
  .mcell.vid .play{{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:34px;height:34px;border-radius:50%;background:rgba(0,0,0,.45);color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px;}}
  .mcell.vid i{{position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.5);color:#fff;font-size:10px;font-style:normal;padding:1px 5px;border-radius:4px;}}
  .feed-react{{background:#f7f7f7;border-radius:8px;padding:8px 10px;margin:8px 0;font-size:13px;}}
  .feed-like{{color:#576b95;padding-bottom:6px;word-break:break-all;}}
  .feed-cmts{{border-top:1px solid #e5e5e5;padding-top:6px;}}
  .feed-cmt{{padding:2px 0;word-break:break-word;}}
  .feed-cmt b{{font-weight:600;color:#576b95;}}
  .avatar{{display:inline-flex;align-items:center;justify-content:center;border-radius:8px;color:#fff;font-weight:700;flex-shrink:0;}}
  .empty{{background:#fff;border-radius:14px;padding:40px 16px;text-align:center;color:#8a8a8a;font-size:14px;}}
  .stat-row{{display:flex;gap:8px;justify-content:center;margin-bottom:14px;flex-wrap:wrap;}}
  .stat-pill{{background:#fff;border-radius:999px;padding:6px 14px;font-size:12px;color:#444;box-shadow:0 1px 3px rgba(0,0,0,.06);}}
  .stat-pill b{{color:#111;margin:0 2px;}}
  footer{{margin-top:26px;text-align:center;font-size:11px;color:#9a9a9a;line-height:2;}}
  footer .tag{{display:inline-block;border:1px solid #dcdcdc;border-radius:999px;padding:1px 12px;margin:2px;background:#fff;}}
</style>
</head>
<body>
<div class="banner">
  <h1>朋友圈</h1>
  <div class="sub">Moments · 时光轴</div>
  <div class="mask"></div>
</div>
<div class="wrap">
  <div class="mine"><span class="my-name">我的朋友圈</span>{avatar_html("我", 48)}</div>
  <div class="stat-row">
    <span class="stat-pill">动态 <b>{total}</b> 条</span>
    <span class="stat-pill">作者 <b>{author_cnt}</b> 人</span>
    <span class="stat-pill">图片/视频 <b>{img_cnt}</b> 个</span>
    <span class="stat-pill">点赞 <b>{like_cnt}</b></span>
    <span class="stat-pill">评论 <b>{cmt_cnt}</b></span>
  </div>
  {empty}
  {feeds}
  <footer>
    <div class="tag">本报告基于 WuShuo 逆向微信协议 Skill 生成</div>
    <div class="tag">由 PagePilot 承载</div>
    <div style="margin-top:8px">© 2026 WuShuo · 本地数据只读分析 · 不涉及任何第三方服务</div>
  </footer>
</div>
</body>
</html>"""
    out_html = args.out or "朋友圈.html"
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"朋友圈渲染: {out_html}")
    print(f"动态 {total} 条 · 作者 {author_cnt} 人 · 图片/视频 {img_cnt} 个 · "
          f"点赞 {like_cnt} · 评论 {cmt_cnt}")


if __name__ == "__main__":
    main()
