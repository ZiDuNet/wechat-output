#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""favorites_export.py — 微信 4.x 收藏导出（直连加密库，高仿微信 UI HTML）

数据源（直连，只读）：
    favorite/favorite.db → fav_db_item（收藏项，content 为 XML，update_time 为时间戳）
    contact/contact.db → fromusr / realchatname → 昵称 映射

type 渲染规则（本机实测 91 条分布）:
    1  文字消息   → desc 正文
    2  图片       → datalist/dataitem（仅显示 CDN 引用 ID，不硬编本地路径）
    3  语音       → 时长 + CDN 引用
    4  视频       → 时长 + CDN 引用
    5  链接       → desc 摘要 + source/link
    6  位置       → locitem（poiname/label/经纬度）
    8  文件       → title 文件名 + datafmt
    14 合并转发   → title + 子项 datatitle
    18 笔记       → 正文 datadesc（自建笔记）
    19 小程序     → title + appbranditem（sourcedisplayname/pagepath）
    20 视频号     → finderFeed（nickname/feedType）

用法：
    # 数据接口（推荐，JSON）
    from favorites_export import FavoritesExporter
    ex = FavoritesExporter(db_dir, keys_file="all_keys.json")   # 或 enc_key="64hex"
    items = ex.fetch(since_ts=..., until_ts=..., limit=100)     # list[dict]
    # 每项: {type, time, src, desc, title, link, items:[{title,desc,size,fmt,dur}],
    #        loc, appbrand, finder}

    # CLI 数据输出（默认 JSON 到 stdout；--out 写文件）
    python favorites_export.py --db-dir <db_storage> --keys <all_keys.json>
    python favorites_export.py --db-dir ... --keys ... --last 1y
    # CLI 渲染（可选）
    python favorites_export.py --db-dir ... --keys ... --out 收藏.html --html
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from xml.etree import ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wcdb_core import WcdbSession, find_db_files, load_keys, get_db_key_for_file  # noqa: E402
from media_common import add_time_args, parse_time_range  # noqa: E402

TYPE_LABEL = {
    1: "文字", 2: "图片", 3: "语音", 4: "视频", 5: "链接",
    6: "位置", 8: "文件", 14: "合并转发", 18: "笔记",
    19: "小程序", 20: "视频号",
}
TYPE_ICON = {
    1: "📝", 2: "🖼️", 3: "🎤", 4: "🎬", 5: "🔗",
    6: "📍", 8: "📎", 14: "💬", 18: "📒",
    19: "🧩", 20: "📹",
}


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


def parse_fav_content(content_xml: str) -> dict:
    r = {"desc": "", "title": "", "link": "", "items": [],
         "loc": None, "appbrand": None, "finder": None}
    if not content_xml:
        return r
    try:
        root = ET.fromstring(content_xml)
    except Exception:
        return r
    r["desc"] = _txt(root, "desc")
    r["title"] = _txt(root, "title")
    src = root.find("source")
    if src is not None:
        r["link"] = _txt(src, "link")
    dl = root.find("datalist")
    if dl is not None:
        for di in dl.findall("dataitem"):
            item = {"title": "", "desc": "", "size": "", "fmt": "", "dur": ""}
            item["title"] = _txt(di, "datatitle")
            item["desc"] = _txt(di, "datadesc")
            sz = _txt(di, "fullsize")
            if sz.isdigit():
                n = int(sz)
                item["size"] = (f"{n / 1024 / 1024:.1f} MB" if n > 1024 * 1024
                                else f"{n / 1024:.0f} KB" if n > 1024 else f"{n} B")
            item["fmt"] = _txt(di, "datafmt")
            dur = _txt(di, "duration")
            if dur.isdigit():
                v = int(dur)
                item["dur"] = f"{v / 1000:.1f}s" if v > 1000 else f"{v}s"
            r["items"].append(item)
    loc = root.find("locitem")
    if loc is not None:
        r["loc"] = {"poiname": _txt(loc, "poiname"), "label": _txt(loc, "label"),
                    "lng": _txt(loc, "lng"), "lat": _txt(loc, "lat")}
    ab = root.find("appbranditem")
    if ab is not None:
        r["appbrand"] = {"name": _txt(ab, "sourcedisplayname"),
                         "pagepath": _txt(ab, "pagepath")}
    ff = root.find("finderFeed")
    if ff is not None:
        r["finder"] = {"nickname": _txt(ff, "nickname"),
                       "feedType": _txt(ff, "feedType")}
    return r


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


def render_item(it: dict, ftype: int) -> str:
    icon = TYPE_ICON.get(ftype, "📦")
    label = TYPE_LABEL.get(ftype, "其他")
    body = ""
    if ftype == 1:
        body = f'<div class="fav-desc">{_esc(it["desc"])}</div>'
    elif ftype == 5:
        body = f'<div class="fav-desc">{_esc(it["desc"])}</div>'
        if it["link"]:
            body += f'<a class="fav-link" href="{_esc(it["link"])}" target="_blank" rel="noopener">🔗 {_esc(it["link"][:80])}</a>'
    elif ftype == 8:
        body = f'<div class="fav-file">📎 {_esc(it["title"] or "文件")}'
        if it["items"]:
            i0 = it["items"][0]
            body += f' <span class="fav-sub">{_esc(i0["fmt"])} {_esc(i0["size"])}</span>'
        body += "</div>"
    elif ftype == 14:
        body = f'<div class="fav-title">{_esc(it["title"] or "聊天记录")}</div>'
        items = "".join(
            f'<div class="fav-item">💬 {_esc(x["title"] or x["desc"] or "")}</div>'
            for x in it["items"][:6])
        if it["items"]:
            body += f'<div class="fav-items">{items}</div>'
    elif ftype == 18:
        body = f'<div class="fav-desc">{_esc(it["desc"] or it["title"])}</div>'
    elif ftype == 19:
        body = f'<div class="fav-title">🧩 {_esc(it["title"] or it["appbrand"]["name"] if it["appbrand"] else it["title"])}</div>'
        if it["appbrand"] and it["appbrand"]["name"]:
            body += f'<div class="fav-sub">小程序：{_esc(it["appbrand"]["name"])}'
            if it["appbrand"]["pagepath"]:
                body += f' · {_esc(it["appbrand"]["pagepath"])}'
            body += "</div>"
    elif ftype == 20:
        body = f'<div class="fav-title">📹 {_esc(it["title"] or "")}</div>'
        if it["finder"]:
            body += f'<div class="fav-sub">视频号：{_esc(it["finder"]["nickname"])}</div>'
    elif ftype == 6:
        if it["loc"]:
            body = (f'<div class="fav-title">📍 {_esc(it["loc"]["poiname"] or "位置")}</div>'
                    f'<div class="fav-sub">{_esc(it["loc"]["label"])} · '
                    f'{_esc(it["loc"]["lat"])},{_esc(it["loc"]["lng"])}</div>')
    elif ftype in (2, 3, 4):
        body = f'<div class="fav-media">'
        for x in it["items"][:3]:
            body += f'<div class="fav-item">{"🖼️" if ftype == 2 else "🎤" if ftype == 3 else "🎬"} ' \
                    f'{_esc(x["title"] or "")} <span class="fav-sub">{_esc(x["size"])} {_esc(x["dur"])}</span></div>'
        body += "</div>"
    if not body:
        body = f'<div class="fav-sub">{_esc(it["desc"])}</div>'
    return (f'<div class="fav"><div class="fav-ic">{icon}</div>'
            f'<div class="fav-bd"><div class="fav-hd"><span class="fav-type">{label}</span>'
            f'<span class="fav-time">{fmt_time(it["time"])}</span></div>{body}</div></div>')


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


class FavoritesExporter:
    """收藏数据接口（直连加密库，只读）。

    from favorites_export import FavoritesExporter
    ex = FavoritesExporter(db_dir, keys_file="all_keys.json")
    items = ex.fetch(since_ts=None, until_ts=None, limit=0)   # 按 update_time 过滤
    """

    def __init__(self, db_dir: str, keys_file: str | None = None,
                 enc_key: str | None = None):
        self.db_dir = db_dir
        self.keys = load_keys(keys_file) if keys_file else {}
        self.enc_key = enc_key
        self._contacts = None

    def _fav_db(self) -> tuple[str, str]:
        cats = find_db_files(self.db_dir)
        if not cats.get("favorite"):
            raise FileNotFoundError("未找到 favorite 库（favorite/favorite.db）")
        fav_db = [f for f in cats["favorite"] if "fts" not in os.path.basename(f)]
        if not fav_db:
            raise FileNotFoundError("favorite 库列表为空")
        fav_db = fav_db[0]
        key = self.enc_key or get_db_key_for_file(fav_db, self.db_dir, self.keys)
        if not key:
            raise RuntimeError("无 favorite 库密钥，请提供 --keys / --key")
        return fav_db, key

    def _contact_names(self) -> dict:
        if self._contacts is not None:
            return self._contacts
        self._contacts = load_contacts(self.db_dir, self.keys)
        return self._contacts

    def fetch(self, since_ts: int | None = None, until_ts: int | None = None,
              limit: int = 0) -> list[dict]:
        """返回收藏结构化数据（按 update_time 倒序）。

        每项字段：type(1-20) time(update_time) src(来源) desc/title/link
                  items([{title,desc,size,fmt,dur}]) loc/appbrand/finder
        """
        fav_db, key = self._fav_db()
        names = self._contact_names()
        items = []
        with WcdbSession(db_path=fav_db, enc_key=key) as db:
            rows = db.query("SELECT local_id, type, update_time, content, fromusr, "
                            "realchatname FROM fav_db_item ORDER BY update_time DESC")
            for r in rows:
                ts = r.get("update_time") or 0
                if since_ts and ts < since_ts:
                    continue
                if until_ts and ts > until_ts:
                    continue
                info = parse_fav_content(r.get("content") or "")
                ftype = r.get("type") or 0
                src = (r.get("realchatname") or "").strip() or (r.get("fromusr") or "").strip()
                items.append({
                    "type": ftype, "time": ts,
                    "src": names.get(src, src) if src else "",
                    **info,
                })
        if limit:
            items = items[:limit]
        return items


def main():
    ap = argparse.ArgumentParser(description="微信收藏数据接口（直连加密库）")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--keys", help="all_keys.json 路径")
    ap.add_argument("--key", help="64hex 统一密钥（无 keys 文件时）")
    ap.add_argument("--out", help="输出文件（默认 stdout JSON）")
    add_time_args(ap)
    ap.add_argument("--limit", type=int, default=0, help="最多返回条数（0=全部）")
    ap.add_argument("--html", action="store_true", help="输出高仿微信 UI HTML（可选）")
    args = ap.parse_args()

    begin_ts, end_ts = parse_time_range(args.since, args.until, args.last)
    ex = FavoritesExporter(args.db_dir, keys_file=args.keys, enc_key=args.key)
    items = ex.fetch(since_ts=begin_ts, until_ts=end_ts, limit=args.limit)

    if not args.html:
        out = args.out or "-"
        payload = {"total": len(items), "items": items}
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        if out == "-":
            print(text)
        else:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
        print(f"收藏数据: {len(items)} 条 -> {out}", file=sys.stderr)
        return

    total = len(items)
    by_type = {}
    for it in items:
        by_type[it["type"]] = by_type.get(it["type"], 0) + 1
    type_cn = " · ".join(f"{TYPE_LABEL.get(k, k)} {v}" for k, v in
                         sorted(by_type.items(), key=lambda x: -x[1])[:6]) or "无"

    cards = "".join(render_item(it, it["type"]) for it in items)
    empty = "" if items else '<div class="empty">该时间范围内没有收藏记录。</div>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>收藏 · 时光胶囊</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{background:#f5f5f5;color:#191919;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.55;}}
  .banner{{background:linear-gradient(180deg,#20293a,#2e3d52 70%,#3a4a5c);color:#fff;text-align:center;padding:28px 16px 46px;position:relative;}}
  .banner h1{{font-size:22px;font-weight:700;letter-spacing:.12em;}}
  .banner .sub{{font-size:12px;color:#aab8c8;margin-top:6px;}}
  .banner .mask{{position:absolute;left:0;right:0;bottom:-1px;height:24px;background:#f5f5f5;border-radius:24px 24px 0 0;}}
  .wrap{{max-width:640px;margin:-28px auto 0;padding:0 12px 50px;position:relative;z-index:2;}}
  .stat-pill{{background:#fff;border-radius:999px;padding:6px 14px;font-size:12px;color:#444;box-shadow:0 1px 3px rgba(0,0,0,.06);display:inline-block;margin-bottom:12px;}}
  .stat-pill b{{color:#111;}}
  .fav{{display:flex;gap:12px;background:#fff;border-radius:12px;padding:14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.05);}}
  .fav-ic{{width:40px;height:40px;border-radius:10px;background:#f1f5f9;display:flex;align-items:center;justify-content:center;font-size:19px;flex-shrink:0;}}
  .fav-bd{{flex:1;min-width:0;}}
  .fav-hd{{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;}}
  .fav-type{{font-size:11px;color:#576b95;background:#eef2fb;border-radius:999px;padding:1px 10px;}}
  .fav-time{{font-size:11px;color:#9a9a9a;}}
  .fav-desc{{font-size:14px;color:#191919;white-space:pre-wrap;word-break:break-word;}}
  .fav-title{{font-size:14px;font-weight:600;color:#111;word-break:break-word;}}
  .fav-sub{{font-size:12px;color:#8a8a8a;margin-top:3px;word-break:break-all;}}
  .fav-link{{display:block;font-size:12px;color:#576b95;margin-top:6px;word-break:break-all;text-decoration:none;}}
  .fav-file{{font-size:14px;color:#111;background:#f7f7f7;border-radius:8px;padding:10px 12px;}}
  .fav-items{{margin-top:6px;}}
  .fav-item{{font-size:13px;color:#374151;padding:4px 0;border-bottom:1px dashed #eee;}}
  .fav-item:last-child{{border-bottom:none;}}
  .fav-media{{margin-top:4px;}}
  .empty{{background:#fff;border-radius:12px;padding:40px 16px;text-align:center;color:#8a8a8a;font-size:14px;}}
  footer{{margin-top:26px;text-align:center;font-size:11px;color:#9a9a9a;line-height:2;}}
  footer .tag{{display:inline-block;border:1px solid #dcdcdc;border-radius:999px;padding:1px 12px;margin:2px;background:#fff;}}
</style>
</head>
<body>
<div class="banner">
  <h1>收藏</h1>
  <div class="sub">Favorites · 时光胶囊</div>
  <div class="mask"></div>
</div>
<div class="wrap">
  <div><span class="stat-pill">共 <b>{total}</b> 条 · {_esc(type_cn)}</span></div>
  {empty}
  {cards}
  <footer>
    <div class="tag">本报告基于 WuShuo 逆向微信协议 Skill 生成</div>
    <div class="tag">由 PagePilot 承载</div>
    <div style="margin-top:8px">© 2026 WuShuo · 本地数据只读分析 · 不涉及任何第三方服务</div>
  </footer>
</div>
</body>
</html>"""
    out_html = args.out or "收藏.html"
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"收藏渲染: {out_html}")
    print(f"收藏 {total} 条 · {type_cn}")


if __name__ == "__main__":
    main()
