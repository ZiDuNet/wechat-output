#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""appmsg_parser.py — 微信 4.x local_type 49 复合消息深度解析（纯解析库，零依赖）

输入 message_content 的 XML 文本，输出结构化 dict；被 chat_analysis.py / export_pay.py
等模块复用。实测确认的 XML 结构（来自 message_0.db 9 万+ 条 49 类 appmsg）：

  【转账】 <appmsg><type>2000</type> + <wcpayinfo>：
      feedesc=金额(带￥) · pay_memo=备注 · paysubtype 1=发起/待收款 3=已收款
      payer_username / receiver_username · transferid / transcationid
  【红包】 <appmsg><type>2001</type> + <wcpayinfo>：
      sendertitle / receivertitle=祝福语 · paymsgid · nativeurl(含 sendusername)
      ⚠️ 本地不存金额（feedesc 为空），金额需微信账单，这里标 None
  【小程序】 <appmsg><type>33</type> + <weappinfo>：
      appid / username(gh_xxx@app) / weappiconurl · 外层 title=分享页标题
      sourcedisplayname / des=小程序名
  【视频号】 <appmsg> + <finderFeed> 节点：desc=动态文案，nickname，url
  【链接卡片】 <appmsg> + ContentObject/普通 appmsg：title / des / url

用法:
    from appmsg_parser import parse_appmsg
    info = parse_appmsg(msg_body(message_row))   # None = 非 49 类/解析失败
    # info = {"kind": "transfer", "amount": "￥1.00", "sub_type": 1,
    #         "payer": "...", "receiver": "...", "memo": "...", "transfer_id": "..."}
"""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

# 常用节点文本提取（不存在返回默认）
def _txt(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    el = node.find(path)
    if el is None or el.text is None:
        return default
    return el.text.strip()


def _txt_i(node: ET.Element | None, path: str, default: str = "") -> str:
    """大小写不敏感路径查找（微信 XML 大小写混用）"""
    if node is None:
        return default
    # 逐段匹配
    parts = [p for p in path.split("/") if p]
    cur: ET.Element | None = node
    for p in parts:
        if cur is None:
            return default
        found = None
        for child in cur:
            if child.tag is not None and child.tag.lower() == p.lower():
                found = child
                break
        cur = found
    if cur is None or cur.text is None:
        return default
    return cur.text.strip()


def _parse(xml: str) -> ET.Element | None:
    if not xml:
        return None
    try:
        return ET.fromstring(xml)
    except Exception:
        return None


def _app_type(root: ET.Element | None) -> int | None:
    if root is None:
        return None
    t = _txt(root, "appmsg/type") or _txt_i(root, "appmsg/type")
    try:
        return int(t)
    except ValueError:
        return None


def parse_transfer(root: ET.Element, pay: ET.Element | None) -> dict:
    """转账：type 2000"""
    return {
        "kind": "transfer",
        "amount": _txt(pay, "feedesc"),
        "memo": _txt(pay, "pay_memo") or _txt(pay, "payMemo"),
        "sub_type": _txt(pay, "paysubtype") or _txt(pay, "paySubType"),  # 1=发起 3=已收款
        "payer": _txt(pay, "payer_username") or _txt(pay, "payerUserName"),
        "receiver": _txt(pay, "receiver_username") or _txt(pay, "receiverUserName"),
        "transfer_id": _txt(pay, "transferid") or _txt(pay, "transferId"),
        "transcation_id": _txt(pay, "transcationid") or _txt(pay, "transcationId"),
        "title": _txt(root, "appmsg/title"),
    }


def parse_redpacket(root: ET.Element, pay: ET.Element | None) -> dict:
    """红包：type 2001（本地不存金额）"""
    return {
        "kind": "redpacket",
        "sender_title": _txt(pay, "sendertitle") or _txt(pay, "senderTitle"),
        "receiver_title": _txt(pay, "receivertitle") or _txt(pay, "receiverTitle"),
        "pay_msgid": _txt(pay, "paymsgid") or _txt(pay, "payMsgId"),
        "native_url": _txt(pay, "nativeurl") or _txt(pay, "nativeUrl"),
        "amount": None,  # 本地不存金额
        "title": _txt(root, "appmsg/title"),
    }


def parse_miniprogram(root: ET.Element, app: ET.Element | None) -> dict:
    """小程序：type 33 + weappinfo"""
    return {
        "kind": "miniprogram",
        "appid": _txt(app, "appid"),
        "gh_username": _txt(app, "username"),
        "icon_url": _txt(app, "weappiconurl") or _txt(app, "weappIconUrl"),
        "page_path": _txt(app, "pagepath") or _txt(app, "pagePath"),
        "name": _txt(app, "sourcedisplayname") or _txt(root, "appmsg/sourcedisplayname")
                or _txt(root, "appmsg/des"),
        "title": _txt(root, "appmsg/title"),
        "des": _txt(root, "appmsg/des"),
    }


def parse_finder(root: ET.Element) -> dict:
    """视频号动态：<finderFeed> 节点（在 appmsg 下）"""
    ff = root.find("appmsg/finderFeed")
    if ff is None:
        appmsg = root.find("appmsg")
        if appmsg is not None:
            for child in appmsg:
                if child.tag is not None and child.tag.lower() == "finderfeed":
                    ff = child
                    break
    return {
        "kind": "finder",
        "desc": _txt(ff, "desc") or _txt(root, "appmsg/title"),
        "nickname": _txt(ff, "nickname"),
        "url": _txt(ff, "url"),
        "feed_type": _txt(ff, "feedType"),
        "title": _txt(root, "appmsg/title"),
    }


def parse_link(root: ET.Element) -> dict:
    """普通链接卡片"""
    return {
        "kind": "link",
        "title": _txt(root, "appmsg/title"),
        "des": _txt(root, "appmsg/des"),
        "url": _txt(root, "appmsg/url")
                or _txt_i(root, "appmsg/ContentObject/contentUrl"),
        "source": _txt(root, "appmsg/sourcedisplayname") or _txt(root, "appmsg/source"),
    }


def parse_appmsg(xml: str) -> dict | None:
    """解析 local_type 49 的 appmsg XML → 结构化 dict；非 49 类/失败返回 None。

    返回 dict 至少含 kind 字段：transfer / redpacket / miniprogram / finder / link。
    """
    if not xml:
        return None
    xml = xml.strip()
    # 微信 4.x 正文带发送者前缀："sender_wxid:\n<xml>"，先剥除再解析
    m = re.match(r"^[A-Za-z0-9_\-]+(?:@[A-Za-z0-9_.\-]+)?:\s*\r?\n", xml)
    if m:
        xml = xml[m.end():]
    root = _parse(xml)
    if root is None or root.tag.lower() != "msg":
        return None
    atype = _app_type(root)
    if atype is None:
        return None
    appmsg = root.find("appmsg")
    pay = appmsg.find("wcpayinfo") if appmsg is not None else None
    if atype == 2000 and pay is not None:
        return parse_transfer(root, pay)
    if atype == 2001 and pay is not None:
        return parse_redpacket(root, pay)
    if atype == 33:
        app = appmsg.find("weappinfo") if appmsg is not None else None
        if app is not None and _txt(app, "appid"):
            return parse_miniprogram(root, app)
    # 视频号：finderFeed 节点（type 多为 56/57 或与普通 appmsg 混用）
    if appmsg is not None:
        for child in appmsg:
            if child.tag is not None and child.tag.lower() == "finderfeed":
                return parse_finder(root)
    # type 57：引用 / 视频分享 / 画布卡片 / 普通分享 分流
    if atype == 57:
        if appmsg is not None and appmsg.find("refermsg") is not None:
            ref = appmsg.find("refermsg")
            return {
                "kind": "reply",
                "quote": _txt(ref, "title") or _txt(root, "appmsg/title"),
                "from": _txt(ref, "chatusr"),
                "title": _txt(root, "appmsg/title"),
            }
        sv = appmsg.find("streamvideo") if appmsg is not None else None
        if sv is not None and (_txt(sv, "streamvideourl") or _txt(sv, "streamvideotitle")):
            return {
                "kind": "video_share",
                "title": _txt(root, "appmsg/title"),
                "dur": _txt(sv, "streamvideototaltime"),
                "url": _txt(sv, "streamvideourl"),
            }
        canvas = False
        if appmsg is not None:
            for child in appmsg:
                if child.tag is not None and child.tag.lower().startswith("canvaspage"):
                    canvas = True
                    break
        if canvas:
            return {"kind": "canvas", "title": _txt(root, "appmsg/title")}
        return parse_link(root)
    if atype in (5, 19, 49, 58, 60, 61, 63, 87, 88):
        return parse_link(root)
    return None


def pay_label(info: dict) -> str:
    """给 chat_analysis 报告用的中文标签"""
    k = info.get("kind")
    if k == "transfer":
        sub = "收款" if str(info.get("sub_type")) == "3" else "转账"
        return f"{sub} {info.get('amount') or ''}".strip()
    if k == "redpacket":
        t = info.get("sender_title") or info.get("receiver_title") or ""
        return f"红包 {t}".strip()
    if k == "miniprogram":
        return f"小程序 {info.get('name') or info.get('title') or ''}".strip()
    if k == "finder":
        return f"视频号 {info.get('desc') or info.get('title') or ''}".strip()
    if k == "reply":
        return f"引用 {info.get('quote') or info.get('title') or ''}".strip()
    if k == "video_share":
        return f"视频 {info.get('title') or ''}".strip()
    if k == "canvas":
        return f"画布卡片 {info.get('title') or ''}".strip()
    return f"链接 {info.get('title') or ''}".strip()


if __name__ == "__main__":
    import sys
    samples = [
        '<msg><appmsg><type>2000</type><title>转账</title>'
        '<wcpayinfo><feedesc>￥1.00</feedesc><pay_memo>奶茶钱</pay_memo>'
        '<paysubtype>3</paysubtype><payer_username>wxid_a</payer_username>'
        '<receiver_username>wxid_b</receiver_username>'
        '<transferid>abc123</transferid><transcationid>t1</transcationid></wcpayinfo></appmsg></msg>',
        '<msg><appmsg><type>2001</type><title>红包</title>'
        '<wcpayinfo><receivertitle>祝生意兴隆</receivertitle>'
        '<paymsgid>p1</paymsgid></wcpayinfo></appmsg></msg>',
        '<msg><appmsg><type>33</type><title>今日天气</title><des>天气小程序</des>'
        '<weappinfo><appid>wxabc</appid><username>gh_123@app</username>'
        '<weappiconurl>http://x/icon.png</weappiconurl><pagepath>pages/a</pagepath>'
        '<sourcedisplayname>天气</sourcedisplayname></weappinfo></appmsg></msg>',
        '<msg><appmsg><type>56</type><title>t</title>'
        '<finderFeed><desc>周末骑行视频</desc><nickname>老张</nickname>'
        '<url>http://finder/v</url></finderFeed></appmsg></msg>',
    ]
    for s in samples:
        print(parse_appmsg(s))
