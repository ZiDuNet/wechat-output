#!/usr/bin/env python3
"""微信 4.x 收藏导出（fav_db_item → Markdown，按 type 分组渲染）

用法:
    python export_favorite.py --dec "<沙盒>\\decrypted" --out "G:\\导出\\收藏.md"
    python export_favorite.py --dec "<沙盒>\\decrypted" --out "..." --last 1y    # 近1年
    python export_favorite.py --dec "<沙盒>\\decrypted" --out "..." --since 2024-01-01

数据源:
    favorite/favorite.db → fav_db_item（收藏项，content 为 XML）
    contact/contact.db → fromusr/realchatname → 昵称 映射

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
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from xml.etree import ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from media_common import add_time_args, parse_time_range  # noqa: E402

# favitem type → 中文标签
TYPE_LABEL = {
    1: "文字", 2: "图片", 3: "语音", 4: "视频", 5: "链接",
    6: "位置", 8: "文件", 14: "合并转发", 18: "笔记",
    19: "小程序", 20: "视频号",
}


def log(msg=""):
    print(msg, flush=True)


def find_db(dec_dir, sub, name):
    p = os.path.join(dec_dir, sub, name)
    if os.path.isfile(p):
        return p
    for root, _d, files in os.walk(dec_dir):
        if name in files:
            return os.path.join(root, name)
    return None


def load_nickname_map(dec_dir):
    """username → 可读名（备注优先，其次昵称）。"""
    db = find_db(dec_dir, "contact", "contact.db")
    if not db:
        return {}
    m = {}
    try:
        conn = sqlite3.connect(db)
        for uname, nick, remark in conn.execute(
                "SELECT username, nick_name, remark FROM contact WHERE username != ''"):
            m[uname] = remark or nick or uname
        conn.close()
    except Exception as e:
        log(f"  [警告] 读取 contact.db 失败: {e}")
    return m


def parse_fav_content(content_xml, row_type):
    """解析 fav_db_item.content XML，按 type 提取展示字段。
    返回 dict: desc, title, link, items(list of {title, desc, size}),
               loc(dict), appbrand(dict), finder(dict)。"""
    r = {"desc": "", "title": "", "link": "", "items": [],
         "loc": None, "appbrand": None, "finder": None}
    if not content_xml:
        return r
    try:
        root = ET.fromstring(content_xml)
    except ET.ParseError:
        return r

    # <desc> 正文（type=1 文字消息 / type=5 链接摘要）
    el = root.find("desc")
    if el is not None and el.text:
        r["desc"] = el.text.strip()

    # <title>（type=8 文件 / type=14 合并转发 / type=19 小程序）
    el = root.find("title")
    if el is not None and el.text:
        r["title"] = el.text.strip()

    # <source><link> 链接（type=5）
    src = root.find("source")
    if src is not None:
        link_el = src.find("link")
        if link_el is not None and link_el.text:
            r["link"] = link_el.text.strip()

    # <datalist><dataitem> 子项
    dl = root.find("datalist")
    if dl is not None:
        for di in dl.findall("dataitem"):
            item = {"title": "", "desc": "", "size": "", "fmt": "", "dur": ""}
            t = di.find("datatitle")
            if t is not None and t.text:
                item["title"] = t.text.strip()
            d = di.find("datadesc")
            if d is not None and d.text:
                item["desc"] = d.text.strip()
            s = di.find("fullsize")
            if s is not None and s.text:
                try:
                    sz = int(s.text)
                    if sz > 1024 * 1024:
                        item["size"] = f"{sz / 1024 / 1024:.1f} MB"
                    elif sz > 1024:
                        item["size"] = f"{sz / 1024:.0f} KB"
                    else:
                        item["size"] = f"{sz} B"
                except ValueError:
                    pass
            fmt = di.find("datafmt")
            if fmt is not None and fmt.text:
                item["fmt"] = fmt.text.strip()
            dur = di.find("duration")
            if dur is not None and dur.text:
                try:
                    v = int(dur.text)
                    if v > 0:
                        item["dur"] = f"{v / 1000:.1f}s" if v > 1000 else f"{v}s"
                except ValueError:
                    pass
            r["items"].append(item)

    # <locitem> 位置（type=6）
    loc = root.find("locitem")
    if loc is not None:
        r["loc"] = {
            "poiname": (loc.findtext("poiname") or "").strip(),
            "label": (loc.findtext("label") or "").strip(),
            "lng": (loc.findtext("lng") or "").strip(),
            "lat": (loc.findtext("lat") or "").strip(),
        }

    # <appbranditem> 小程序（type=19）
    ab = root.find("appbranditem")
    if ab is not None:
        r["appbrand"] = {
            "name": (ab.findtext("sourcedisplayname") or "").strip(),
            "pagepath": (ab.findtext("pagepath") or "").strip(),
        }

    # <finderFeed> 视频号（type=20）
    ff = root.find("finderFeed")
    if ff is not None:
        r["finder"] = {
            "nickname": (ff.findtext("nickname") or "").strip(),
            "feedType": (ff.findtext("feedType") or "").strip(),
        }

    return r


def main():
    ap = argparse.ArgumentParser(description="微信 4.x 收藏导出为 Markdown（按 type 分组）")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--out", required=True, help="输出 Markdown 路径")
    add_time_args(ap)
    args = ap.parse_args()

    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)

    fav_db = find_db(args.dec, "favorite", "favorite.db")
    if not fav_db:
        log("[错误] 未找到 favorite.db，请确认 --dec 指向解密库目录")
        sys.exit(1)
    log(f"[i] 收藏库: {fav_db}")

    log("[i] 加载联系人昵称映射...")
    nick_map = load_nickname_map(args.dec)
    log(f"    映射 {len(nick_map)} 个联系人")

    # 读取全部收藏
    log("[i] 读取收藏列表...")
    conn = sqlite3.connect(fav_db)
    rows = conn.execute(
        "SELECT local_id, type, update_time, fromusr, realchatname, content "
        "FROM fav_db_item").fetchall()
    conn.close()
    log(f"    共 {len(rows)} 条原始记录")

    # 解析 + 时间过滤
    items = []
    skipped_time = 0
    skipped_parse = 0
    for lid, rtype, utime, fromusr, realchatname, content in rows:
        if not utime:
            skipped_parse += 1
            continue
        utime = int(utime)
        if since_ts and utime < since_ts:
            skipped_time += 1
            continue
        if until_ts and utime > until_ts:
            skipped_time += 1
            continue
        parsed = parse_fav_content(content, rtype)
        # 来源人昵称
        who = nick_map.get(fromusr, fromusr) if fromusr else "自己"
        # 来源会话（群聊时 realchatname 更准）
        chat = nick_map.get(realchatname, realchatname) if realchatname else ""
        items.append({
            "lid": lid, "type": rtype, "time": utime,
            "who": who, "fromusr": fromusr, "chat": chat,
            **parsed,
        })

    # 按时间倒序
    items.sort(key=lambda x: x["time"], reverse=True)
    log(f"[i] 解析成功 {len(items)} 条（时间过滤跳过 {skipped_time}，无时间跳过 {skipped_parse}）")

    # type 统计
    from collections import Counter
    type_dist = Counter(it["type"] for it in items)

    # 写 Markdown
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# 微信收藏导出\n\n")
        f.write(f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        if since_ts or until_ts:
            s = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d") if since_ts else "最早"
            e = datetime.fromtimestamp(until_ts).strftime("%Y-%m-%d") if until_ts else "最新"
            f.write(f"- 时间范围：{s} ~ {e}\n")
        f.write(f"- 共 {len(items)} 条收藏\n\n")
        # type 统计
        f.write("## 类型统计\n\n")
        f.write("| 类型 | 数量 |\n|------|------|\n")
        for t, cnt in sorted(type_dist.items()):
            label = TYPE_LABEL.get(t, f"未知({t})")
            f.write(f"| {label} | {cnt} |\n")
        f.write("\n---\n\n")

        for it in items:
            ts = datetime.fromtimestamp(it["time"]).strftime("%Y-%m-%d %H:%M")
            label = TYPE_LABEL.get(it["type"], f"未知类型({it['type']})")
            who_label = it["who"]
            if it["chat"] and it["chat"] != it["who"]:
                who_label += f"（来自会话：{it['chat']}）"
            f.write(f"## {ts} · {label} · {who_label}\n\n")

            # 按类型渲染
            if it["type"] == 1:
                # 文字消息
                if it["desc"]:
                    f.write(f"{it['desc']}\n\n")
            elif it["type"] == 2:
                # 图片
                f.write("📷 图片收藏")
                if it["items"]:
                    itm = it["items"][0]
                    if itm["size"]:
                        f.write(f"（{itm['size']}）")
                f.write("（原图需联网或从本地媒体目录查找，此处仅索引）\n\n")
            elif it["type"] == 3:
                # 语音
                itm = it["items"][0] if it["items"] else {}
                dur = itm.get("dur", "")
                f.write(f"🎤 语音{('（' + dur + '）') if dur else ''}\n\n")
            elif it["type"] == 4:
                # 视频
                itm = it["items"][0] if it["items"] else {}
                dur = itm.get("dur", "")
                size = itm.get("size", "")
                f.write(f"🎬 视频{('（' + dur + '）') if dur else ''}"
                        f"{('（' + size + '）') if size else ''}\n\n")
            elif it["type"] == 5:
                # 链接
                if it["desc"]:
                    f.write(f"{it['desc']}\n\n")
                if it["link"]:
                    title = it["title"] or "链接"
                    f.write(f"🔗 [{title}]({it['link']})\n\n")
                elif it["items"] and it["items"][0]["title"]:
                    f.write(f"🔗 {it['items'][0]['title']}\n\n")
            elif it["type"] == 6:
                # 位置
                loc = it["loc"]
                if loc:
                    f.write(f"📍 **{loc['poiname']}**\n\n")
                    if loc["label"]:
                        f.write(f"{loc['label']}\n\n")
                    if loc["lat"] and loc["lng"]:
                        f.write(f"坐标：{loc['lat']}, {loc['lng']}\n\n")
            elif it["type"] == 8:
                # 文件
                if it["title"]:
                    f.write(f"📎 **{it['title']}**\n\n")
                if it["items"]:
                    itm = it["items"][0]
                    parts = []
                    if itm["fmt"]:
                        parts.append(f"格式：{itm['fmt']}")
                    if itm["size"]:
                        parts.append(f"大小：{itm['size']}")
                    if parts:
                        f.write("　".join(parts) + "\n\n")
            elif it["type"] == 14:
                # 合并转发
                if it["title"]:
                    f.write(f"📋 **{it['title']}**\n\n")
                for itm in it["items"]:
                    if itm["title"] and itm["desc"]:
                        f.write(f"- {itm['title']}：{itm['desc']}\n")
                    elif itm["title"]:
                        f.write(f"- {itm['title']}\n")
                    elif itm["desc"]:
                        f.write(f"- {itm['desc']}\n")
                if it["items"]:
                    f.write("\n")
            elif it["type"] == 18:
                # 笔记
                for itm in it["items"]:
                    if itm["desc"]:
                        f.write(f"{itm['desc']}\n\n")
                if it["desc"]:
                    f.write(f"{it['desc']}\n\n")
            elif it["type"] == 19:
                # 小程序
                if it["title"]:
                    f.write(f"📱 **{it['title']}**\n\n")
                if it["appbrand"]:
                    ab = it["appbrand"]
                    if ab["name"]:
                        f.write(f"来自：{ab['name']}\n\n")
                    if ab["pagepath"]:
                        f.write(f"路径：`{ab['pagepath']}`\n\n")
                if it["desc"]:
                    f.write(f"{it['desc']}\n\n")
            elif it["type"] == 20:
                # 视频号
                if it["finder"]:
                    fd = it["finder"]
                    f.write(f"📺 视频号：{fd['nickname']}")
                    if fd["feedType"]:
                        f.write(f"（类型 {fd['feedType']}）")
                    f.write("\n\n")
                if it["desc"]:
                    f.write(f"{it['desc']}\n\n")
            else:
                # 未知类型兜底
                f.write(f"（未识别类型 {it['type']}）\n\n")
                if it["desc"]:
                    f.write(f"{it['desc']}\n\n")
                if it["title"]:
                    f.write(f"{it['title']}\n\n")

            f.write("---\n\n")

    log(f"[完成] 已导出 {len(items)} 条收藏 → {args.out}")
    dist_str = ", ".join(f"{TYPE_LABEL.get(t, t)}={c}" for t, c in sorted(type_dist.items()))
    log(f"  类型分布：{dist_str}")


if __name__ == "__main__":
    main()
