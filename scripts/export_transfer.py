#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转账 / 红包 / 小程序分享 导出（微信 4.x）

本机实测确认的 XML 结构（grep 自 message_0.db 的 90675 条 49 类 appmsg）：

【转账】<appmsg><type>2000</type>，<wcpayinfo> 块：
  - <feedesc>￥1.00</feedesc>   金额（带￥）
  - <pay_memo>备注</pay_memo>    转账备注
  - <paysubtype>1/3</paysubtype> 1=发起/待收款，3=已收款（同一 transferid 会出现两条）
  - <payer_username> / <receiver_username>
  - <transferid> / <transcationid>
【红包】<appmsg><type>2001</type>，<wcpayinfo> 块：
  - <receivertitle>/<sendertitle>  祝福语（如"祝XX生意兴隆"）
  - <paymsgid> / <nativeurl> 里 sendusername=发红包人
  - ⚠️ 本地不存金额（feedesc 为空），金额需去微信账单查；这里标"未解析"
【小程序】<appmsg><type>33</type>，<weappinfo> 块（appid 非空才算）：
  - <appid> / <username>(gh_xxx@app) / <weappiconurl>
  - 外层 <title>=分享页标题，<sourcedisplayname>/<des>=小程序名

用法:
    python export_transfer.py --dec "<沙盒>\\decrypted" --out "G:\\导出\\转账红包小程序.md"
    python export_transfer.py --dec "<沙盒>\\decrypted" --last 3m --out "近三月转账.md"
    python export_transfer.py --dec "<沙盒>\\decrypted" --kind transfer --out "只导转账.md"
"""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
PREFIX_RE = re.compile(r"^([A-Za-z0-9_\-]+(?:@[A-Za-z0-9_.\-]+)?):\r?\n")


def zstd_decode(blob):
    if not blob:
        return ""
    if isinstance(blob, str):
        return blob
    if blob[:4] == ZSTD_MAGIC:
        try:
            import zstandard
            return zstandard.ZstdDecompressor().decompress(blob).decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        return blob.decode("utf-8", "replace")
    except Exception:
        return ""


def xml_text(xml, tag):
    m = re.search(r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (tag, tag), xml, re.S)
    return m.group(1).strip() if m else ""


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def classify_and_extract(xml):
    """判断一条 appmsg 是 红包/转账/小程序/其它，返回 (kind, dict) 或 (None, {})。"""
    app_type = xml_text(xml, "type")
    title = xml_text(xml, "title")

    # ---- 红包：type=2001 或 title 含"红包"
    if app_type == "2001" or ("红包" in title and "wcpayinfo" in xml.lower()):
        blessing = xml_text(xml, "sendertitle") or xml_text(xml, "receivertitle") or ""
        paymsgid = xml_text(xml, "paymsgid")
        # nativeurl 里 sendusername=xxx
        m = re.search(r"sendusername=([^&\"<]+)", xml)
        sender = m.group(1) if m else ""
        des = xml_text(xml, "des")
        return "红包", {
            "blessing": blessing or "(无祝福语)",
            "sender": sender,
            "paymsgid": paymsgid,
            "amount": "未解析（本地不存金额）",
            "status": "未解析",
            "des": des,
        }

    # ---- 转账：type=2000 或 wcpayinfo 含 transferid
    wcpay = re.search(r"<wcpayinfo>(.*?)</wcpayinfo>", xml, re.S)
    if app_type == "2000" or (wcpay and xml_text(wcpay.group(1), "transferid")):
        blk = wcpay.group(1) if wcpay else xml
        amount = xml_text(blk, "feedesc") or "未解析"
        memo = xml_text(blk, "pay_memo") or ""
        paysub = xml_text(blk, "paysubtype")
        status_map = {"1": "发起/待收款", "3": "已收款"}
        status = status_map.get(paysub, f"未解析(paysubtype={paysub or '?'})")
        return "转账", {
            "amount": amount,
            "memo": memo or "(无备注)",
            "payer": xml_text(blk, "payer_username"),
            "receiver": xml_text(blk, "receiver_username"),
            "transferid": xml_text(blk, "transferid"),
            "status": status,
        }

    # ---- 小程序：weappinfo 且 appid 非空
    m_app = re.search(r"<weappinfo>(.*?)</weappinfo>", xml, re.S)
    if m_app and re.search(r"<appid>[^<\[]", m_app.group(1)):
        blk = m_app.group(1)
        appid = xml_text(blk, "appid")
        appuser = xml_text(blk, "username")
        icon = xml_text(blk, "weappiconurl")
        app_name = xml_text(xml, "sourcedisplayname") or xml_text(xml, "des") or "(未知小程序)"
        return "小程序", {
            "title": xml_text(xml, "title") or "(无标题)",
            "app_name": app_name,
            "appid": appid,
            "appuser": appuser,
            "icon": icon,
        }

    return None, {}


def main():
    ap = argparse.ArgumentParser(description="转账/红包/小程序分享导出")
    ap.add_argument("--dec", required=True, help="解密库目录(decrypted)")
    ap.add_argument("--out", required=True, help="输出 Markdown 路径")
    ap.add_argument("--kind", choices=["transfer", "redpacket", "miniapp", "all"],
                    default="all", help="只导某一类：transfer=转账 redpacket=红包 miniapp=小程序 all=全部")
    from media_common import add_time_args, parse_time_range
    add_time_args(ap)
    args = ap.parse_args()

    since_ts, until_ts = parse_time_range(args.since, args.until, args.last)
    msg_dir = os.path.join(args.dec, "message")
    db_files = sorted(
        os.path.join(msg_dir, f) for f in os.listdir(msg_dir)
        if re.match(r"^message_\d+\.db$", f))
    if not db_files:
        sys.exit(f"[x] {msg_dir} 下没有 message_N.db")

    records = []   # dict: ts, session, sender, kind, detail
    stat = {"转账": 0, "红包": 0, "小程序": 0, "跳过": 0}

    for db_path in db_files:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # 本库 Name2Id：rowid → user_name；并预计算 md5(username) → 会话
        try:
            n2i = {r["rowid"]: r["user_name"]
                   for r in conn.execute("SELECT rowid, user_name FROM Name2Id")}
        except Exception:
            n2i = {}
        md2sess = {hashlib.md5(u.encode()).hexdigest(): u for u in n2i.values()}

        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        for t in tables:
            sess = md2sess.get(t.replace("Msg_", ""), "(未知会话)")
            sql = f"SELECT create_time, real_sender_id, message_content FROM {t} WHERE (local_type&255)=49"
            cond, params = [], []
            if since_ts:
                cond.append("create_time >= ?"); params.append(since_ts)
            if until_ts:
                cond.append("create_time <= ?"); params.append(until_ts)
            if cond:
                sql += " AND " + " AND ".join(cond)
            try:
                rows = conn.execute(sql, params).fetchall()
            except Exception:
                continue
            for row in rows:
                raw = zstd_decode(row["message_content"])
                if not raw:
                    continue
                # 剥 "<发件人>:\n" 前缀，得到纯 XML
                m = PREFIX_RE.match(raw)
                sender = m.group(1) if m else ""
                xml = raw[m.end():] if m else raw
                kind, info = classify_and_extract(xml)
                if kind is None:
                    stat["跳过"] += 1
                    continue
                if args.kind != "all" and {
                    "transfer": "转账", "redpacket": "红包", "miniapp": "小程序"}[args.kind] != kind:
                    continue
                records.append({
                    "ts": row["create_time"], "session": sess, "sender": sender,
                    "kind": kind, "info": info,
                })
                stat[kind] += 1
        conn.close()
        print(f"  [i] 已扫描 {os.path.basename(db_path)}（累计 转账{stat['转账']}/红包{stat['红包']}/小程序{stat['小程序']}）")

    # 写 Markdown
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    lines = ["# 转账 / 红包 / 小程序分享 导出", ""]
    lines.append(f"- 导出时间：{fmt_ts(datetime.now().timestamp())}")
    rng = []
    if since_ts: rng.append(f"起 {fmt_ts(since_ts)}")
    if until_ts: rng.append(f"止 {fmt_ts(until_ts)}")
    lines.append(f"- 时间范围：{' '.join(rng) if rng else '全部'}")
    lines.append(f"- 类型过滤：{args.kind}")
    lines.append("")
    lines.append("## 汇总表")
    lines.append("")
    lines.append("| 时间 | 会话 | 类型 | 关键信息 |")
    lines.append("|---|---|---|---|")

    def brief(kind, info):
        if kind == "转账":
            return f"{info['amount']} {info['status']} 备注:{info['memo']}"
        if kind == "红包":
            return f"祝福:{info['blessing']} 发送人:{info['sender'] or '?'}"
        if kind == "小程序":
            return f"{info['app_name']} - {info['title']}"
        return ""

    for r in sorted(records, key=lambda x: x["ts"]):
        lines.append(f"| {fmt_ts(r['ts'])} | {r['session']} | {r['kind']} | {brief(r['kind'], r['info'])} |")
    lines.append("")
    lines.append("## 明细")
    lines.append("")
    for r in sorted(records, key=lambda x: x["ts"]):
        lines.append(f"### {fmt_ts(r['ts'])}  [{r['kind']}]  会话={r['session']}  发件人={r['sender'] or '(自己/未知)'}")
        for k, v in r["info"].items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n[ok] 转账 {stat['转账']} 条，红包 {stat['红包']} 条，小程序 {stat['小程序']} 条"
          f"（未识别 {stat['跳过']} 条）-> {args.out}")


if __name__ == "__main__":
    main()
