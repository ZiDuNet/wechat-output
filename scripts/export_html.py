#!/usr/bin/env python3
"""export_html.py — HTML 聊天档案导出（直连加密库读消息，只读）

把指定会话渲染成单个离线可打开的 HTML：文字/图片/视频/语音/系统消息分卡片，
图片用硬链接解析后复制到同名 assets/ 目录（HTML 引用相对路径），
可在无微信环境直接浏览。不改动微信任何库。

用法:
    python export_html.py --db-dir <db_storage> --keys <all_keys.json> \
        --session <会话username> --out out/聊天.html
    python export_html.py --db-dir <db_storage> --keys <all_keys.json> \
        --session <会话username> --out out/聊天.html --account-dir <微信账号目录> \
        --limit 5000
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
from datetime import datetime

from msg_reader import MessageReader, msg_body, extract_md5s, LOCAL_TYPE_LABEL, try_zstd
from hardlink import HardlinkResolver
from wcdb_core import load_keys


def render_text(text: str) -> str:
    return html.escape(text, quote=False).replace("\n", "<br>")


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name)


class HtmlExporter:
    """HTML 聊天档案导出器"""

    def __init__(self, db_dir: str, enc_key: str | None = None,
                 keys_file: str | None = None):
        self._reader = MessageReader(db_dir, enc_key, keys_file)

    def export(self, out_html: str, session_id: str | None = None,
               account_dir: str | None = None, limit: int | None = None,
               begin_ts: int | None = None, end_ts: int | None = None,
               hardlink: bool = False) -> dict:
        """导出会话 HTML。返回摘要 dict"""
        out_html = os.path.abspath(out_html)
        os.makedirs(os.path.dirname(out_html), exist_ok=True)
        assets_dir = os.path.join(os.path.dirname(out_html),
                                  safe_name(os.path.splitext(os.path.basename(out_html))[0]) + "_assets")
        os.makedirs(assets_dir, exist_ok=True)

        resolver = HardlinkResolver(self._reader._db_dir, self._reader._enc_key,
                                    self._reader._keys) if account_dir else None
        copy = shutil.copy2 if not hardlink else os.link

        counts = {"text": 0, "image": 0, "video": 0, "voice": 0, "other": 0}
        tokens = []
        n = 0
        for r in self._reader.iter_messages(session_id, begin_ts, end_ts):
            if limit and n >= limit:
                break
            n += 1
            body = msg_body(r)
            local_type = r.get("local_type")
            label = LOCAL_TYPE_LABEL.get(local_type, f"type{local_type}")
            counts[label if label in counts else "other"] += 1
            ts = r.get("create_time")
            ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
            sender_id = r.get("real_sender_id")

            rendered = ""
            if local_type == 1:
                rendered = "<div class='txt'>" + render_text(body) + "</div>"
            elif local_type == 3:
                md5s = extract_md5s(body).get("img", [])
                mds = []
                for md in md5s:
                    info = resolver.resolve_image(md, account_dir) if resolver else None
                    if info and info.get("file_path") and os.path.exists(info["file_path"]):
                        rel = safe_name(md) + "." + os.path.splitext(info["file_path"])[1].lstrip(".")
                        dst = os.path.join(assets_dir, rel)
                        if not os.path.exists(dst):
                            try:
                                copy(info["file_path"], dst)
                            except OSError:
                                dst = info["file_path"]
                        mds.append(f"<img class='img' src='{html.escape(rel, quote=True)}' />")
                    else:
                        mds.append("<div class='img'>⚠️ 图片文件缺失</div>")
                rendered = "".join(mds) if mds else render_text(body)[:500]
            elif local_type == 43:
                md5s_ = extract_md5s(body).get("video", [])
                vs = []
                for md in md5s_:
                    info = resolver.resolve_video(md) if resolver else None
                    if info and info.get("file_path") and os.path.exists(info["file_path"]):
                        rel = safe_name(md) + os.path.splitext(info["file_path"])[1]
                        dst = os.path.join(assets_dir, rel)
                        if not os.path.exists(dst):
                            try:
                                copy(info["file_path"], dst)
                            except OSError:
                                dst = info["file_path"]
                        vs.append(f"<video class='media' controls src='{html.escape(rel, quote=True)}'></video>")
                rendered = "".join(vs) if vs else render_text(body)[:500]
            else:
                text = body if not isinstance(body, bytes) else body.decode("utf-8", errors="replace")
                rendered = render_text(text[:2000]) if text.strip() else "(空)"

            tokens.append((ts_str, sender_id, label, rendered))

        # 单页 HTML 输出
        cards = []
        for ts_str, sid, label, rendered in tokens:
            cards.append(
                f'<div class="msg"><div class="meta"><span class="ts">{html.escape(ts_str)}</span>'
                f'<span class="who">{html.escape(str(sid or "?"))}</span>'
                f'<span class="type">{html.escape(label)}</span></div><div class="body">{rendered}</div></div>'
            )
        page = PAGE_TEMPLATE.replace("{{TITLE}}", html.escape(session_id or "全部会话"))
        page = page.replace("{{COUNT}}", str(n))
        page = page.replace("{{CARDS}}", "\n".join(cards))
        with open(out_html, "w", encoding="utf-8") as f:
            f.write(page)
        return {"out": out_html, "assets": assets_dir, "messages": n, "counts": counts}


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
  body{font-family:-apple-system,'Microsoft YaHei',sans-serif;max-width:860px;margin:0 auto;padding:16px;background:#f7f7f7;color:#222}
  h1{font-size:20px}
  .msg{background:#fff;border-radius:8px;padding:10px 14px;margin:8px 0;box-shadow:0 1px 2px rgba(0,0,0,.08)}
  .meta{font-size:12px;color:#999;margin-bottom:4px}
  .meta span{margin-right:10px}
  .who{color:#0b6bcb}
  .txt{font-size:15px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
  .img,.media{max-width:100%;border-radius:6px;display:block;margin-top:6px}
  .type{background:#eef2f7;border-radius:4px;padding:1px 6px}
  .foot{color:#999;font-size:12px;text-align:center;margin:24px 0}
</style>
</head>
<body>
<h1>{{TITLE}}</h1>
<p>共 {{COUNT}} 条消息。图片/视频素材在 assets 子目录。</p>
{{CARDS}}
<div class="foot">由 wechat-output · export_html.py 生成</div>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="HTML 聊天档案导出（只读）")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="密钥（64位hex，所有库共用）")
    ap.add_argument("--keys", help="all_keys.json 路径（每个库独立密钥）")
    ap.add_argument("--session", help="限定会话 username（不传则导出全部会话）")
    ap.add_argument("--out", required=True, help="输出 .html 文件路径")
    ap.add_argument("--account-dir", help="微信账号目录（用于定位图片/视频实际文件）")
    ap.add_argument("--limit", type=int, help="最大消息条数")
    ap.add_argument("--begin", help="起始时间 YYYY-MM-DD")
    ap.add_argument("--end", help="结束时间 YYYY-MM-DD")
    ap.add_argument("--hardlink", action="store_true", help="媒体用硬链接而非复制（需同盘）")
    args = ap.parse_args()

    begin_ts = end_ts = None
    if args.begin:
        begin_ts = int(datetime.strptime(args.begin, "%Y-%m-%d").timestamp())
    if args.end:
        end_ts = int((datetime.strptime(args.end, "%Y-%m-%d") + datetime.timedelta(days=1)).timestamp())

    exporter = HtmlExporter(args.db_dir, args.key, args.keys)
    summary = exporter.export(args.out, session_id=args.session,
                              account_dir=args.account_dir, limit=args.limit,
                              begin_ts=begin_ts, end_ts=end_ts, hardlink=args.hardlink)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()