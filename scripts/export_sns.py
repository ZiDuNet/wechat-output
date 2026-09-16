#!/usr/bin/env python3
"""微信 4.x 朋友圈导出（SnsTimeLine → Markdown，含评论/点赞）

用法:
    python export_sns.py --dec "<沙盒>\\decrypted" --out "G:\\导出\\朋友圈.md"
    python export_sns.py --dec "<沙盒>\\decrypted" --out "..." --last 3m      # 近3个月
    python export_sns.py --dec "<沙盒>\\decrypted" --out "..." --since 2025-01-01 --until 2025-12-31

数据源:
    sns/sns.db → SnsTimeLine（朋友圈正文，content 为 XML）
               → SnsMessage_tmp3（别人对你/你对别人朋友圈的评论与点赞）
    contact/contact.db → username → 昵称/备注 映射

说明:
    - contentDesc 为朋友圈正文；图片/视频 URL 从 XML 的 mediaList 提取（通常是 http(s) 网络地址）；
    - 地点从 location 标签的 poiName 属性提取；分享文章从 ContentObject 的 title/contentUrl 提取。
    - 评论/点赞按 feed_id（= SnsTimeLine.tid）关联，附在对应朋友圈下方。
    - 本机实测：SnsTimeLine 395 行，评论 59 行（feed_id↔tid 命中率 43/44）。
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from xml.etree import ElementTree as ET

# 昵称/正文常含 emoji；stdout 重定向到管道时 Python 退回 GBK，替换而非崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from media_common import add_time_args, parse_time_range  # noqa: E402


def log(msg=""):
    print(msg, flush=True)


def find_db(dec_dir, sub, name):
    """在解密库目录下定位某个 db 文件。"""
    p = os.path.join(dec_dir, sub, name)
    if os.path.isfile(p):
        return p
    # 兜底：递归找
    for root, _d, files in os.walk(dec_dir):
        if name in files:
            return os.path.join(root, name)
    return None


def load_nickname_map(dec_dir):
    """从 contact.db 加载 username → 可读名映射（备注优先，其次昵称）。"""
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


def load_comments(dec_dir):
    """从 SnsMessage_tmp3 加载评论/点赞，按 feed_id 分组。
    返回 {feed_id: [ {type, from_nick, content, time}, ... ]}
    type: 1=赞, 2=评论。"""
    db = find_db(dec_dir, "sns", "sns.db")
    if not db:
        return {}
    groups = {}
    try:
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT feed_id, type, from_nickname, content, create_time "
            "FROM SnsMessage_tmp3 ORDER BY create_time ASC").fetchall()
        conn.close()
    except Exception as e:
        log(f"  [警告] 读取 SnsMessage_tmp3 失败: {e}")
        return {}
    for fid, typ, fnick, content, ct in rows:
        groups.setdefault(fid, []).append({
            "type": typ,
            "from": fnick or "",
            "content": content or "",
            "time": ct or 0,
        })
    return groups


def parse_sns_content(content_xml):
    """解析 SnsTimeLine.content XML，提取结构化字段。
    返回 dict: time, desc, nickname, location, media(list), share_title, share_url。"""
    result = {
        "time": 0, "desc": "", "nickname": "", "location": "",
        "media": [], "share_title": "", "share_url": "",
    }
    if not content_xml:
        return result
    try:
        root = ET.fromstring(content_xml)
    except ET.ParseError:
        return result

    # TimelineObject 节点
    tlo = root.find("TimelineObject")
    if tlo is None:
        return result

    # 时间
    el = tlo.find("createTime")
    if el is not None and el.text:
        try:
            result["time"] = int(el.text)
        except ValueError:
            pass

    # 正文
    el = tlo.find("contentDesc")
    if el is not None and el.text:
        result["desc"] = el.text.strip()

    # 地点（location 标签的 poiName 属性）
    loc = tlo.find("location")
    if loc is not None:
        poi = loc.get("poiName", "").strip()
        if poi:
            result["location"] = poi

    # ContentObject：图片/视频/分享
    co = tlo.find("ContentObject")
    if co is not None:
        # 分享文章（type=3 等）
        title_el = co.find("title")
        url_el = co.find("contentUrl")
        if title_el is not None and title_el.text:
            result["share_title"] = title_el.text.strip()
        if url_el is not None and url_el.text:
            result["share_url"] = url_el.text.strip()

        # 媒体列表
        media_list = co.find("mediaList")
        if media_list is not None:
            for media in media_list.findall("media"):
                mtype_el = media.find("type")
                mtype = mtype_el.text if mtype_el is not None else ""
                url_el = media.find("url")
                url = url_el.text.strip() if url_el is not None and url_el.text else ""
                thumb_el = media.find("thumb")
                thumb = thumb_el.text.strip() if thumb_el is not None and thumb_el.text else ""
                dur_el = media.find("videoDuration")
                dur = ""
                if dur_el is not None and dur_el.text:
                    try:
                        d = float(dur_el.text)
                        if d > 0:
                            dur = f"{d:.1f}s"
                    except ValueError:
                        pass
                if url:
                    # media/type: 2=图片, 6=视频
                    kind = "视频" if mtype == "6" else "图片"
                    result["media"].append({"kind": kind, "url": url, "thumb": thumb, "dur": dur})

    # LocalExtraInfo.nickname（发信人昵称，有时比 contact 表更准）
    lei = root.find("LocalExtraInfo")
    if lei is not None:
        nn = lei.find("nickname")
        if nn is not None and nn.text:
            result["nickname"] = nn.text.strip()

    return result


def render_comments(comments):
    """把一组评论/点赞渲染为 Markdown 子块。"""
    lines = []
    likes = [c for c in comments if c["type"] == 1]
    cmts = [c for c in comments if c["type"] == 2]
    if likes:
        names = "、".join(c["from"] or "有人" for c in likes)
        lines.append(f"  - 👍 赞：{names}（{len(likes)}人）")
    for c in cmts:
        who = c["from"] or "有人"
        text = c["content"].replace("\n", " ").strip()
        lines.append(f"  - 💬 {who}：{text}")
    return lines


def main():
    ap = argparse.ArgumentParser(
        description="微信 4.x 朋友圈导出为 Markdown（含评论/点赞）")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--out", required=True, help="输出 Markdown 路径")
    add_time_args(ap)
    args = ap.parse_args()

    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)

    # 定位 sns.db
    sns_db = find_db(args.dec, "sns", "sns.db")
    if not sns_db:
        log("[错误] 未找到 sns.db，请确认 --dec 指向解密库目录")
        sys.exit(1)
    log(f"[i] 朋友圈库: {sns_db}")

    # 加载昵称映射 + 评论
    log("[i] 加载联系人昵称映射...")
    nick_map = load_nickname_map(args.dec)
    log(f"    映射 {len(nick_map)} 个联系人")

    log("[i] 加载评论/点赞...")
    comments_map = load_comments(args.dec)
    total_cmts = sum(len(v) for v in comments_map.values())
    log(f"    {len(comments_map)} 条朋友圈有评论，共 {total_cmts} 条评论/赞")

    # 遍历 SnsTimeLine
    log("[i] 读取朋友圈时间线...")
    conn = sqlite3.connect(sns_db)
    rows = conn.execute(
        "SELECT tid, user_name, content FROM SnsTimeLine").fetchall()
    conn.close()
    log(f"    共 {len(rows)} 条原始记录")

    # 解析 + 时间过滤
    items = []
    skipped_time = 0
    skipped_parse = 0
    for tid, user_name, content in rows:
        info = parse_sns_content(content)
        if info["time"] == 0:
            skipped_parse += 1
            continue
        # 时间过滤
        if since_ts and info["time"] < since_ts:
            skipped_time += 1
            continue
        if until_ts and info["time"] > until_ts:
            skipped_time += 1
            continue
        # 发信人昵称：优先 XML 内 nickname，其次 contact 映射，最后 user_name
        who = info["nickname"] or nick_map.get(user_name, user_name)
        items.append({
            "tid": tid,
            "who": who,
            "user_name": user_name,
            "time": info["time"],
            "desc": info["desc"],
            "location": info["location"],
            "media": info["media"],
            "share_title": info["share_title"],
            "share_url": info["share_url"],
            "comments": comments_map.get(tid, []),
        })

    # 按时间倒序
    items.sort(key=lambda x: x["time"], reverse=True)

    log(f"[i] 解析成功 {len(items)} 条（时间过滤跳过 {skipped_time}，无时间戳跳过 {skipped_parse}）")

    # 写 Markdown
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# 微信朋友圈导出\n\n")
        f.write(f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        if since_ts or until_ts:
            s = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d") if since_ts else "最早"
            e = datetime.fromtimestamp(until_ts).strftime("%Y-%m-%d") if until_ts else "最新"
            f.write(f"- 时间范围：{s} ~ {e}\n")
        f.write(f"- 共 {len(items)} 条朋友圈\n\n")
        f.write("---\n\n")

        for it in items:
            ts = datetime.fromtimestamp(it["time"]).strftime("%Y-%m-%d %H:%M")
            f.write(f"## {ts} · {it['who']}\n\n")
            if it["desc"]:
                f.write(f"{it['desc']}\n\n")
            if it["location"]:
                f.write(f"📍 {it['location']}\n\n")
            # 分享文章
            if it["share_url"]:
                title = it["share_title"] or "分享链接"
                f.write(f"🔗 [{title}]({it['share_url']})\n\n")
            # 媒体（只渲染 http(s) 网络 URL；非 URL 的内部引用不画图）
            for m in it["media"]:
                if not m["url"].startswith(("http://", "https://")):
                    continue
                if m["kind"] == "图片":
                    f.write(f"![图片]({m['url']})\n\n")
                else:
                    dur = f"（{m['dur']}）" if m["dur"] else ""
                    f.write(f"🎬 视频{dur}：{m['url']}\n\n")
            # 评论/点赞
            if it["comments"]:
                f.write("<details><summary>评论与点赞</summary>\n\n")
                for line in render_comments(it["comments"]):
                    f.write(line + "\n")
                f.write("\n</details>\n\n")
            f.write("---\n\n")

    log(f"[完成] 已导出 {len(items)} 条朋友圈 → {args.out}")
    # 统计
    img_cnt = sum(len([m for m in it["media"] if m["kind"] == "图片"]) for it in items)
    vid_cnt = sum(len([m for m in it["media"] if m["kind"] == "视频"]) for it in items)
    loc_cnt = sum(1 for it in items if it["location"])
    cmt_cnt = sum(len(it["comments"]) for it in items)
    log(f"  统计：图片 {img_cnt} 张，视频 {vid_cnt} 个，带位置 {loc_cnt} 条，评论/赞 {cmt_cnt} 条")


if __name__ == "__main__":
    main()
