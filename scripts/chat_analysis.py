#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聊天记录深度分析（群聊/私聊通用，只读直连加密库）——可固化的周期报告引擎。

UI 参照 ChatLab（github.com/ChatLab/ChatLab）风格：920px 窄布局、卡片、彩虹环形饼图、
主题渐变柱图、金/银/铜排名进度条、右侧锚点导航、渐变赛季大标题。

维度：
  - 总览 Overview：消息量/成员/活跃/日均/峰值/类型/成员分布
  - 洞察 Insights：类型构成+文本深度、时间规律（小时/星期/热力图/夜猫子）、话题聚类与演变、互动关系网络
  - 榜单 Rankings：发言总榜、@互动排行、邻近度排行（矩阵热力图+对榜）、最火复读、口头禅、含笑量、关键词词云

周期模式（固化报告用）：
  --period daily|weekly|monthly|custom
    daily   = 今天 00:00 ~ 现在（当日报告）
    weekly  = 最近 7 天（含今天）
    monthly = 最近 30 天
    custom  = --since/--until 指定
  --out-dir 输出目录，自动命名 <显示名>_<周期>_<YYYYMMDD>.html
  --sessions 支持逗号分隔批量生成（如 "群A,群B,wxid_xxx"）

用法：
  python chat_analysis.py --db-dir <db_storage> --keys <all_keys.json> \
      --session "北清路TT" --period monthly --out-dir <报告目录> --json
"""
import argparse
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

SCRIPT_INTERFACE = "CLI"

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from msg_reader import MessageReader
    from wcdb_core import WcdbSession
except Exception as _e:  # pragma: no cover
    print("依赖导入失败:", _e, file=sys.stderr)
    sys.exit(2)

# ---------- 类型标签（local_type 语义，与 SKILL.md 一致） ----------
TYPE_LABELS = [
    (1, "文本"), (3, "图片"), (34, "语音"), (43, "视频"), (47, "表情"),
    (48, "位置"), (49, "链接/复合"), (10000, "系统通知"), (10002, "撤回"),
    (266287972401, "拍一拍"), (244813135921, "引用"),
]
TYPE_FALLBACK = "其他"

# ---------- 通用话题词典（多标签命中） ----------
TOPICS = {
    "工作/职场": ["工作", "上班", "开会", "加班", "老板", "同事", "项目", "客户", "面试",
              "辞职", "工资", "绩效", "周报", "需求", "会议", "出差", "请假", "值班", "打卡"],
    "技术/数码": ["代码", "bug", "电脑", "手机", "显卡", "cpu", "gpu", "服务器", "docker",
              "linux", "python", "java", "app", "软件", "更新", "系统", "网络", "配置",
              "接口", "数据库", "部署", "报错", "前端", "后端"],
    "美食/生活": ["吃", "好吃", "火锅", "烧烤", "奶茶", "咖啡", "饭", "菜", "早餐", "晚餐",
              "夜宵", "外卖", "蛋糕", "零食", "饿", "喝", "啤酒", "白酒", "聚餐", "美食"],
    "游戏/娱乐": ["游戏", "王者", "原神", "麻将", "打牌", "钓鱼", "刷剧", "电影", "综艺",
              "直播", "steam", "排位", "剧本杀", "斗地主", "上分"],
    "运动/车": ["跑步", "健身", "骑行", "摩托", "车", "装备", "公里", "头盔", "锻炼", "游泳",
              "篮球", "足球", "爬山", "徒步", "驾照", "保险", "违章", "加油", "续航", "轮胎"],
    "理财/投资": ["基金", "股票", "黄金", "理财", "收益", "涨", "跌", "套", "币", "大盘",
              "a股", "美股", "加仓", "减仓", "回本", "赚", "亏", "上市", "价格"],
    "情感/家庭": ["对象", "结婚", "分手", "相亲", "孩子", "爸妈", "买房", "装修", "搬家",
              "婚礼", "彩礼", "老婆", "老公", "女朋友", "男朋友", "家里"],
    "健康/养生": ["睡觉", "熬夜", "失眠", "减肥", "体检", "血压", "养生", "吃药", "医院",
              "生病", "感冒", "发烧", "嗓子", "腰", "颈椎", "眼睛", "头疼"],
}
STOPWORDS = set("的了是在我有你不就都也啊嗯哦吧吗呢这那什么一个哈哈哈哈哈哈哈哈哈卧槽我靠牛逼真的知道不知道没有")
STOPWORDS.update(["哈哈哈", "哈哈哈哈", "哈哈哈哈哈", "卧槽", "我靠", "真的", "牛逼", "不知道",
                  "没有", "emm", "emmm", "哦哦", "嗯嗯", "对啊", "是的", "嗯呢", "图片", "视频"])
# 微信表情中文名（词云/关键词过滤：表情不是关键词）
WECHAT_EMOJI = {
    "微笑", "撇嘴", "色", "发呆", "得意", "流泪", "害羞", "闭嘴", "睡", "大哭", "尴尬", "发怒",
    "调皮", "呲牙", "惊讶", "难过", "酷", "冷汗", "抓狂", "吐", "偷笑", "可爱", "白眼", "傲慢",
    "饥饿", "困", "惊恐", "流汗", "憨笑", "大兵", "奋斗", "咒骂", "疑问", "嘘", "晕", "折磨",
    "衰", "骷髅", "敲打", "再见", "擦汗", "抠鼻", "鼓掌", "糗大了", "坏笑", "左哼哼", "右哼哼",
    "哈欠", "鄙视", "委屈", "快哭了", "阴险", "亲亲", "吓", "可怜", "菜刀", "西瓜", "啤酒",
    "篮球", "乒乓", "咖啡", "饭", "猪头", "玫瑰", "凋谢", "示爱", "爱心", "心碎", "蛋糕",
    "闪电", "炸弹", "刀", "足球", "便便", "月亮", "太阳", "礼物", "拥抱", "强", "弱", "握手",
    "胜利", "抱拳", "勾引", "拳头", "差劲", "爱你", "爱情", "飞吻", "跳跳", "发抖", "怄火",
    "转圈", "磕头", "回头", "跳绳", "挥手", "激动", "街舞", "献吻", "左太极", "右太极", "双喜",
    "鞭炮", "灯笼", "发财", "唱歌", "购物", "邮件", "帅", "喝彩", "祈祷", "爆筋", "棒棒糖",
    "喝奶", "下面", "香蕉", "飞机", "开车", "左车头", "车厢", "右车头", "多云", "下雨", "钞票",
    "熊猫", "灯泡", "风车", "闹钟", "打伞", "彩球", "钻戒", "沙发", "纸巾", "药", "手枪",
    "螃蟹", "喵喵", "蹭一蹭", "白狗", "求抱抱", "发红包", "干杯", "馋嘴", "疲惫", "感动",
    "加油", "苦涩", "翻白眼", "敷衍", "让我看看", "叹气", "哇", "破涕为笑", "捂脸", "呲牙笑",
    "掩面", "旺柴", "社会社会", "托腮", "皱眉", "好笑", "裂开", "捂嘴", "哭泣", "泪奔",
    "捂脸哭", "生气", "无奈", "心心眼", "色眯眯", "流口水", "略略略", "污", "害羞笑", "捂肚子",
    "笑哭", "汗", "无语", "呵呵", "哈哈", "吃瓜", "好的", "收到", "点赞", "比心", "666",
    "龇牙", "偷笑", "奸笑", "呲牙笑", "捂脸笑", "哭了", "大哭", "流泪", "撇嘴", "微笑",
}
STOPWORDS |= WECHAT_EMOJI
# 高频口语噪音（聊天过渡词，非话题词）
STOPWORDS |= {
    "没事", "不是", "可以", "我的", "走了", "好了", "对吧", "是吧", "不行", "不对", "什么",
    "这样", "那样", "怎么", "为什么", "因为", "所以", "然后", "还有", "就是", "这个", "那个",
    "现在", "时候", "已经", "一个", "人家", "知道", "看看", "哈哈", "好吧", "行了", "来了",
    "回来", "出去", "今天", "明天", "昨天", "晚上", "早上", "下午", "中午", "真的", "确实",
    "有点", "一点", "一下", "我们", "你们", "他们", "自己", "起来", "下去", "过来", "过去",
    "没有", "有没有", "是不是", "行不行", "可以了", "好的", "ok", "OK", "嗯", "啊", "哦",
}
LOW_NOISE = {"[破涕为笑]", "[捂脸]", "[旺柴]", "[裂开]", "[Emm]", "[奸笑]", "[偷笑]", "[呲牙]", "[坏笑]", "[吃瓜]"}
AT_RE = re.compile(r"@([^\s@:，。！？!?\n]{1,40})")
PREFIX_RE = re.compile(r"^[A-Za-z0-9_@.\-]{2,64}:\n")
LAUGH_RE = re.compile(r"(哈哈+|嘿嘿+|嘻嘻+|笑死|233+|666+|hhh+|😂|🤣)")

# ChatLab 主题色板
PIE_COLORS = ["#6366f1", "#8b5cf6", "#ec4899", "#f43f5e", "#f97316",
              "#eab308", "#22c55e", "#14b8a6", "#06b6d4", "#3b82f6"]
RANK_BARS = [  # 1/2/3 名金/银/铜渐变，其余主题粉
    "linear-gradient(90deg,#fbbf24,#f59e0b)",
    "linear-gradient(90deg,#d1d5db,#9ca3af)",
    "linear-gradient(90deg,#fb923c,#d97706)",
    "linear-gradient(90deg,#ee4567,#f7758c)",
]


# ---------- 工具 ----------
def try_zstd(b: bytes) -> bytes:
    if b[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard
            return zstandard.ZstdDecompressor().decompress(b)
        except Exception:
            return b""
    return b


def clean_text(raw) -> str:
    """原始消息内容 → 可读文本（剥离前缀/XML/表情噪音）"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        if raw[:4] == b"\x28\xb5\x2f\xfd":
            raw = try_zstd(raw)
        try:
            s = raw.decode("utf-8", "replace")
        except Exception:
            return ""
    else:
        s = str(raw)
    m = PREFIX_RE.match(s)
    if m:
        s = s[m.end():]
    s = s.strip()
    if s.startswith("<") and ("<msg>" in s[:50] or "<emoji" in s[:50]):
        return ""
    if "拍了拍" in s and len(s) < 50:
        return ""
    if re.match(r"^https?://\S+$", s):
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\[[^\]]{1,6}\]", "", s)   # 删除微信表情标记 [捂脸] 等
    s = s.replace("\r", " ").replace("\n", " ").strip()
    if s in LOW_NOISE:
        return ""
    return s


def type_label(lt: int) -> str:
    if lt in (10000, 10002) or lt >= (1 << 30):
        for k, v in TYPE_LABELS:
            if lt == k:
                return v
        return "复合类型"
    for k, v in TYPE_LABELS:
        if (lt & 255) == k:
            return v
    return TYPE_FALLBACK


# ---------- 数据采集 ----------
class ChatAnalyzer:
    def __init__(self, db_dir: str, keys_file: str | None = None, enc_key: str | None = None):
        self.db_dir = db_dir
        self.reader = MessageReader(db_dir, enc_key=enc_key, keys_file=keys_file)
        self.names = self._load_contact_names()

    def _load_contact_names(self) -> dict:
        names = {}
        for db_path in self._contact_dbs():
            key = self.reader._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    for r in db.query("SELECT username, nick_name, remark FROM contact"):
                        u = r.get("username") or ""
                        if not u:
                            continue
                        disp = (r.get("remark") or "").strip() or (r.get("nick_name") or "").strip()
                        if disp:
                            names[u] = disp
            except Exception:
                pass
        return names

    def _contact_dbs(self) -> list:
        dbs = []
        for root, _dirs, files in os.walk(self.db_dir):
            for name in files:
                if name.startswith("contact") and name.endswith(".db") \
                        and not name.endswith(("-wal", "-shm")) and "fts" not in name:
                    dbs.append(os.path.join(root, name))
        return sorted(dbs)

    def resolve_session(self, session: str) -> tuple[str, str]:
        """输入 username/群名/昵称 → (username, display_name)。多候选报错退出。"""
        s = session.strip()
        if s.endswith("@chatroom") or re.match(r"^wxid_\w+$", s) or "@openim" in s:
            return s, self.names.get(s, s)
        hits = [(u, n) for u, n in self.names.items() if n and s in n]
        if not hits:
            print(f"[!] 未找到匹配「{session}」的会话。试试传入 username（如 xxx@chatroom / wxid_xxx）。",
                  file=sys.stderr)
            sys.exit(2)
        if len(hits) > 1:
            cands = "\n".join(f"  {n}  ->  {u}" for u, n in hits[:10])
            print(f"[!] 「{session}」命中 {len(hits)} 个候选，请用 username 精确指定：\n{cands}",
                  file=sys.stderr)
            sys.exit(2)
        return hits[0][0], hits[0][1]

    def group_member_count(self, room_username: str) -> int | None:
        if not room_username.endswith("@chatroom"):
            return None
        for db_path in self._contact_dbs():
            key = self.reader._get_key(db_path)
            if not key:
                continue
            try:
                with WcdbSession(db_path=db_path, enc_key=key) as db:
                    tabs = [r["name"] for r in db.query(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='chatroom_member'")]
                    if not tabs:
                        continue
                    rid = db.query("SELECT rowid FROM name2id WHERE username = ?", (room_username,))
                    if rid:
                        n = db.query("SELECT COUNT(*) AS c FROM chatroom_member WHERE room_id = ?",
                                     (rid[0]["rowid"],))
                        return n[0]["c"] if n else None
            except Exception:
                pass
        return None

    def scan(self, session: str, begin_ts: int | None, end_ts: int | None,
             top: int = 10) -> dict:
        texts = []            # (dt, sender_disp, text)
        types = Counter()
        media = Counter()
        at_pairs, refer_pairs, pat_pairs = Counter(), Counter(), Counter()
        n_total = 0
        senders = Counter()
        active_days = defaultdict(set)
        hour_cnt = Counter()
        week_cnt = Counter()
        night = Counter()
        day_cnt = Counter()
        hw = Counter()        # (hour, weekday) 热力图

        for r in self.reader.iter_messages(session_id=session, begin_ts=begin_ts, end_ts=end_ts):
            n_total += 1
            lt = r.get("local_type") or 0
            ts = r.get("create_time") or 0
            dt = datetime.fromtimestamp(ts)
            src_u = r.get("_sender_user") or ""
            src = self.names.get(src_u, "我" if not src_u else src_u)
            if lt not in (10000, 10002):
                senders[src] += 1
                active_days[src].add(dt.strftime("%Y-%m-%d"))
            day_cnt[dt.strftime("%Y-%m-%d")] += 1
            hour_cnt[dt.hour] += 1
            wd = dt.weekday()  # 0=Mon
            week_cnt[wd] += 1
            hw[(dt.hour, wd)] += 1
            if dt.hour >= 23 or dt.hour < 5:
                night[src] += 1

            low = lt & 255
            if low == 3:
                media["image"] += 1
            elif low == 43:
                media["video"] += 1
            elif low == 34:
                media["voice"] += 1
            elif low == 49:
                media["link"] += 1
            types[type_label(lt)] += 1

            if lt in (10000, 10002):
                continue
            s = clean_text(r.get("message_content"))
            if not s:
                continue
            if (low == 1 or lt == 0) and len(s) >= 1:
                texts.append((dt, src, s))
                for mm in AT_RE.findall(s):
                    t = mm.strip()
                    if t and len(t) >= 2 and not t.startswith("http"):
                        at_pairs[(src, t)] += 1
            if "<refermsg>" in s:
                cu = re.search(r"<chatusr>([^<]+)</chatusr>", s)
                if cu:
                    tgt = self.names.get(cu.group(1).strip(), cu.group(1).strip())
                    refer_pairs[(src, tgt)] += 1
            if "拍了拍" in s:
                mm = re.search(r"([^\s]{1,30})\s*拍了拍\s*([^\s]{1,30})", s)
                if mm:
                    pat_pairs[(mm.group(1), mm.group(2))] += 1

        return self._summarize(session, texts, types, media, at_pairs, refer_pairs,
                               pat_pairs, senders, active_days, hour_cnt, week_cnt,
                               night, day_cnt, hw, n_total, begin_ts, end_ts, top)

    def _summarize(self, session, texts, types, media, at_pairs, refer_pairs, pat_pairs,
                   senders, active_days, hour_cnt, week_cnt, night, day_cnt, hw,
                   n_total, begin_ts, end_ts, top):
        lens = sorted(len(t[2]) for t in texts)
        L = len(lens)
        if L:
            def pct(p):
                return lens[min(L - 1, int(L * p))]
            len_stats = {"n": L, "avg": round(sum(lens) / L, 1), "median": lens[L // 2],
                         "p25": pct(0.25), "p75": pct(0.75), "p90": pct(0.90),
                         "max": lens[-1],
                         "short": round(sum(1 for x in lens if x <= 10) / L * 100, 1),
                         "long": sum(1 for x in lens if x >= 100)}
        else:
            len_stats = {"n": 0, "avg": 0, "median": 0, "p25": 0, "p75": 0, "p90": 0,
                         "max": 0, "short": 0, "long": 0}

        # 话题聚类（多标签）+ 周演变 + 代表消息
        topic_cnt = Counter()
        topic_weeks = defaultdict(Counter)
        topic_msgs = defaultdict(list)
        week0 = datetime.fromtimestamp(begin_ts or 0)
        for dt, src, s in texts:
            tl = s.lower()
            for topic, kws in TOPICS.items():
                if any(kw in tl for kw in kws):
                    topic_cnt[topic] += 1
                    wk = ((dt.date() - week0.date()).days // 7) if week0 else 0
                    lbl = (week0 + timedelta(days=wk * 7)).strftime("%m-%d")
                    topic_weeks[lbl][topic] += 1
                    if len(topic_msgs[topic]) < 40 and 15 <= len(s) <= 120:
                        topic_msgs[topic].append((dt.strftime("%m-%d %H:%M"), src, s))
        topic_quotes = {}
        for t, items in topic_msgs.items():
            seen, picked = set(), []
            for it in sorted(items, key=lambda x: -len(x[2])):
                if it[2] in seen:
                    continue
                seen.add(it[2])
                picked.append(it)
                if len(picked) >= 3:
                    break
            topic_quotes[t] = picked

        # 复读
        dup = Counter(t[2] for t in texts if len(t[2]) >= 4)
        dup_top = [{"text": k, "count": v} for k, v in dup.most_common(10) if v >= 5]

        # 口头禅（每人 top1）
        catch = {}
        for _dt, src, s in texts:
            words = re.findall(r"[\u4e00-\u9fff]{2,6}", s)
            cnt = Counter(w for w in words if w not in STOPWORDS)
            for w, c in cnt.most_common(2):
                catch.setdefault(src, Counter())[w] += c
        catch_top = sorted(
            ({'name': k, 'word': w, 'count': c}
             for k, cnt in catch.items() for w, c in cnt.most_common(1)),
            key=lambda x: -x['count'])[:10]

        # 含笑量
        laugh = Counter()
        laugh_total = 0
        msgs_by = Counter()
        for _dt, src, s in texts:
            msgs_by[src] += 1
            c = len(LAUGH_RE.findall(s))
            if c:
                laugh[src] += c
                laugh_total += c
        laugh_top = [{"name": k, "count": v, "rate": round(v / msgs_by.get(k, 1) * 100, 1)}
                     for k, v in laugh.most_common(10)]

        # 榜单
        sender_rank = [{"name": k, "count": v,
                        "days": len(active_days[k]),
                        "rate": round(v / n_total * 100, 1) if n_total else 0}
                       for k, v in senders.most_common(top)]
        at_from_cnt, at_to_cnt = Counter(), Counter()
        for (a, b), v in at_pairs.items():
            at_from_cnt[a] += v
            at_to_cnt[b] += v
        at_from = [{"name": k, "count": v} for k, v in at_from_cnt.most_common(top)]
        at_to = [{"name": k, "count": v} for k, v in at_to_cnt.most_common(top)]
        # 邻近度：@×1 + 引用×2 + 拍一拍×1.5
        intimacy = Counter()
        for (a, b), v in at_pairs.items():
            intimacy[(a, b)] += v
        for (a, b), v in refer_pairs.items():
            intimacy[(a, b)] += v * 2
        for (a, b), v in pat_pairs.items():
            intimacy[(a, b)] += int(v * 1.5)
        inti_top = [{"from": k[0], "to": k[1], "score": v}
                    for k, v in intimacy.most_common(top * 3) if v >= 3][:top]

        # 互动网络（graph 数据：@ 提及，count>=3）
        graph_links = [{"from": k[0], "to": k[1], "count": v}
                       for k, v in at_pairs.most_common(60) if v >= 3]

        # 关键词
        kw = Counter()
        for _dt, _src, s in texts:
            for w in re.findall(r"[\u4e00-\u9fff]{2,6}", s):
                if w not in STOPWORDS:
                    kw[w] += 1
        kw_top = [{"word": k, "count": v} for k, v in kw.most_common(40) if v >= 3]

        # 时间画像
        peak_hour = max(hour_cnt.items(), key=lambda x: x[1])[0] if hour_cnt else 0
        peak_wd = max(week_cnt.items(), key=lambda x: x[1])[0] if week_cnt else 0
        wd_total = sum(week_cnt.values())
        weekend_ratio = round((week_cnt.get(5, 0) + week_cnt.get(6, 0)) / wd_total * 100, 1) if wd_total else 0

        # 成员分布（TOP10 + 其他）
        mem_pie = [{"name": k, "value": v} for k, v in senders.most_common(10)]
        other = sum(v for k, v in senders.most_common()[10:])
        if other > 0:
            mem_pie.append({"name": "其他成员", "value": other})

        # 邻近度矩阵（Top 20 成员 × 成员）
        top_members = [k for k, _ in senders.most_common(20)]
        names_idx = {n: i for i, n in enumerate(top_members)}
        pair_map = defaultdict(int)
        for (a, b), v in intimacy.items():
            pair_map[(a, b)] += v
        hw_matrix = []
        for i in range(24):
            row = []
            for j in range(7):
                row.append(hw.get((i, j), 0))
            hw_matrix.append(row)
        inti_matrix = []
        max_inti = 1
        for i, a in enumerate(top_members):
            row = []
            for j, b in enumerate(top_members):
                if i == j:
                    row.append(0)
                else:
                    v = pair_map.get((a, b), 0) + pair_map.get((b, a), 0)
                    max_inti = max(max_inti, v)
                    row.append(v)
            inti_matrix.append(row)

        return {
            "session": session,
            "n_total": n_total,
            "n_text": L,
            "senders": len(senders),
            "active_days": len(day_cnt),
            "avg_day": round(n_total / max(1, len(day_cnt)), 1),
            "peak_day": max(day_cnt.items(), key=lambda x: x[1]) if day_cnt else ("", 0),
            "span_days": max(1, (datetime.fromtimestamp(end_ts or 0) -
                                 datetime.fromtimestamp(begin_ts or 0)).days + 1),
            "types": dict(types),
            "media": dict(media),
            "len_stats": len_stats,
            "hour_cnt": dict(hour_cnt),
            "week_cnt": {str(k): v for k, v in week_cnt.items()},
            "peak_hour": peak_hour,
            "peak_wd": peak_wd,
            "weekend_ratio": weekend_ratio,
            "night_total": sum(night.values()),
            "night_top": [{"name": k, "count": v} for k, v in night.most_common(8)],
            "hw_matrix": hw_matrix,
            "topic_cnt": dict(topic_cnt.most_common()),
            "topic_weeks": {k: dict(v) for k, v in sorted(topic_weeks.items())},
            "topic_quotes": topic_quotes,
            "dup_top": dup_top,
            "catch_top": catch_top,
            "laugh_total": laugh_total,
            "laugh_top": laugh_top,
            "sender_rank": sender_rank,
            "mem_pie": mem_pie,
            "at_from": at_from,
            "at_to": at_to,
            "intimacy_top": inti_top,
            "inti_matrix": inti_matrix,
            "inti_members": top_members,
            "graph_links": graph_links,
            "kw_top": kw_top,
        }


# ---------- HTML 输出（ChatLab 风格） ----------
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _stat_cards(items):
    """ChatLab StatCard：label + 值（彩色）+ emoji 图标淡底 + subtext"""
    parts = []
    for label, value, icon, color, sub in items:
        parts.append(
            f'<div class="stat"><div class="stat-ic" style="background:{color}1a">{icon}</div>'
            f'<div class="stat-body"><div class="stat-lb">{label}</div>'
            f'<div class="stat-v" style="color:{color}">{value}</div>'
            f'<div class="stat-sb">{sub}</div></div></div>')
    return '<div class="stat-grid">' + "".join(parts) + "</div>"


def _rank_rows(rows, unit=""):
    """ChatLab RankList：排名/名字/值/渐变进度条/百分比"""
    out = []
    maxv = rows[0]["value"] if rows else 1
    for i, r in enumerate(rows):
        pct = round(r["value"] / maxv * 100) if maxv else 0
        rank_cls = ["text-amber", "text-silver", "text-bronze"][i] if i < 3 else "text-muted"
        bar = RANK_BARS[i] if i < 3 else RANK_BARS[3]
        out.append(
            f'<div class="rk"><span class="rk-no {rank_cls}">{i + 1:02d}</span>'
            f'<div class="rk-body"><div class="rk-top"><span class="rk-name" title="{_esc(r["name"])}">{_esc(r["name"])}</span>'
            f'<span class="rk-val">{r["value"]:,}<i>{unit}</i></span></div>'
            f'<div class="rk-bar"><div style="width:{pct}%;background:{bar}"></div></div></div>'
            f'<span class="rk-pct">{pct}%</span></div>')
    return '<div class="rk-list">' + "".join(out) + "</div>"


def _type_progress(types, total):
    """ChatLab 类型摘要：色点 + 名称 + 进度条 + 数量(百分比)"""
    items = sorted(types.items(), key=lambda x: -x[1])
    out = []
    for i, (name, cnt) in enumerate(items):
        pct = round(cnt / total * 100) if total else 0
        color = PIE_COLORS[i % len(PIE_COLORS)]
        out.append(
            f'<div class="tp"><span class="tp-dot" style="background:{color}"></span>'
            f'<span class="tp-name">{_esc(name)}</span>'
            f'<div class="tp-bar"><div style="width:{pct}%;background:{color}"></div></div>'
            f'<span class="tp-cnt">{cnt:,}<i>({pct}%)</i></span></div>')
    return '<div class="tp-list">' + "".join(out) + "</div>"


def _table(headers, rows, num_cols=(1,)):
    th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    trs = []
    for row in rows:
        tds = []
        for i, cell in enumerate(row):
            cls = ' class="num"' if i in num_cols else ""
            tds.append(f"<td{cls}>{_esc(cell)}</td>")
        trs.append("<tr>" + "".join(tds) + "</tr>")
    return f'<div class="tbl-wrap"><table><tr>{th}</tr>{"".join(trs)}</table></div>'


def _section(sid, title, note, body, emoji=""):
    return (f'<section id="{sid}" class="card-sec">'
            f'<div class="sec-hd"><h2>{emoji} {title}</h2>'
            f'<p class="sec-note">{note}</p></div>{body}</section>')


def build_html(data: dict, display: str, top: int, period: str, generated: str) -> str:
    d = data
    weeks = list(d["topic_weeks"].keys())
    topics = list(d["topic_cnt"].keys())
    WK_COLORS = ["#6366f1", "#ec4899", "#f59e0b", "#14b8a6", "#8b5cf6", "#f43f5e"]

    ls = d["len_stats"]
    types_total = sum(d["types"].values()) or 1
    mem_other = sum(x["value"] for x in d["mem_pie"][10:])
    period_cn = {"daily": "日报", "weekly": "周报", "monthly": "月报"}.get(period, "自定义周期")

    chart = {
        "types": [{"name": k, "value": v} for k, v in sorted(d["types"].items(), key=lambda x: -x[1])],
        "mem": d["mem_pie"],
        "len": [ls["p25"], ls["median"], ls["p75"], ls["p90"], ls["max"]],
        "hours": [d["hour_cnt"].get(i, 0) for i in range(24)],
        "hours_lbl": [f"{i:02d}" for i in range(24)],
        "week": [d["week_cnt"].get(str(i), 0) for i in range(7)],
        "week_cn": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
        "hw": d["hw_matrix"],
        "topics": [{"name": k, "value": v} for k, v in d["topic_cnt"].items()],
        "weeks": weeks,
        "week_series": [{
            "name": t, "type": "line", "smooth": True,
            "data": [d["topic_weeks"].get(w, {}).get(t, 0) for w in weeks],
            "itemStyle": {"color": WK_COLORS[i % len(WK_COLORS)]},
            "lineStyle": {"color": WK_COLORS[i % len(WK_COLORS)], "width": 2.5},
            "symbolSize": 6,
        } for i, t in enumerate(topics)],
        "inti_members": d["inti_members"],
        "inti_matrix": d["inti_matrix"],
        "graph": {"nodes": [], "links": []},
    }
    node_deg = Counter()
    for lk in d["graph_links"]:
        node_deg[lk["from"]] += lk["count"]
        node_deg[lk["to"]] += lk["count"]
    for name, deg in node_deg.most_common(18):
        chart["graph"]["nodes"].append({"name": name, "value": deg})
    for lk in d["graph_links"]:
        if lk["from"] in node_deg and lk["to"] in node_deg:
            chart["graph"]["links"].append(
                {"source": lk["from"], "target": lk["to"], "value": lk["count"]})

    js = json.dumps(chart, ensure_ascii=False)

    quotes_html = ""
    for t, items in list(d["topic_quotes"].items())[:5]:
        q = "".join(f'<div class="quote"><span class="who">[{_esc(it[0])}] {_esc(it[1])}：</span>{_esc(it[2])}</div>'
                    for it in items)
        quotes_html += f'<h4 class="sub-t">{_esc(t)}</h4>{q}'

    kw_spans = "".join(
        f'<span title="{_esc(w["word"])} × {w["count"]}" data-i="{i}">{_esc(w["word"])}</span>'
        for i, w in enumerate(d["kw_top"]))

    len_rows = [["平均长度", f'{ls["avg"]} 字', "P75 分位", f'{ls["p75"]} 字'],
                ["中位数", f'{ls["median"]} 字', "P90 分位", f'{ls["p90"]} 字'],
                ["P25 分位", f'{ls["p25"]} 字', "最长单条", f'{ls["max"]} 字'],
                ["短文占比（≤10字）", f'{ls["short"]}%', "长文（≥100字）", f'{ls["long"]} 条']]

    wk_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d["peak_wd"]]
    nm = d.get("members")
    member_note = f"群成员 {nm} 人 · " if nm else ""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(display)} · {period_cn} · 聊天深度分析</title>
<style>
  :root{{--card:#fff;--ink:#111827;--sub:#6b7280;--line:#e5e7eb;--bg:#f5f6f8;--page:#f5f6f8;--pink:#ee4567;}}
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{background:var(--page);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6;}}
  .wrap{{max-width:1280px;margin:0 auto;padding:0 20px 60px;}}
  header{{background:linear-gradient(120deg,#111827,#1f2937 45%,#312e81);color:#fff;padding:38px 0 30px;margin-bottom:22px;}}
  header .inner{{max-width:1180px;margin:0 auto;padding:0 24px;}}
  header .kicker{{font-size:11px;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:#a5b4fc;}}
  header h1{{font-size:30px;font-weight:800;letter-spacing:.5px;margin-top:6px;}}
  header .meta{{margin-top:10px;font-size:13px;color:#cbd5e1;}}
  header .badges{{margin-top:14px;}}
  header .badge{{display:inline-block;margin:0 8px 4px 0;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.22);border-radius:999px;padding:3px 12px;font-size:12px;color:#e2e8f0;}}
  .layout{{display:flex;gap:22px;align-items:flex-start;max-width:1180px;margin:0 auto;}}
  aside.anchors{{position:sticky;top:18px;width:150px;flex-shrink:0;display:none;}}
  aside.anchors .border-l{{border-left:1px solid var(--line);}}
  aside.anchors a{{display:block;border-left:2px solid transparent;margin-left:-1px;padding:7px 0 7px 14px;font-size:13px;color:var(--sub);text-decoration:none;transition:all .15s;}}
  aside.anchors a:hover{{color:#111827;}}
  aside.anchors a.on{{border-left-color:var(--pink);color:var(--pink);font-weight:600;}}
  main.main-content{{flex:1;min-width:0;max-width:920px;margin:0 auto;}}
  @media(min-width:1100px){{aside.anchors{{display:block;}}}}
  .card-sec{{background:var(--card);border:1px solid rgba(229,231,235,.7);border-radius:16px;box-shadow:0 1px 2px rgba(0,0,0,.03);transition:box-shadow .3s;margin-bottom:20px;}}
  .card-sec:hover{{box-shadow:0 10px 24px rgba(0,0,0,.08);}}
  .sec-hd{{padding:16px 22px 0;}}
  .sec-hd h2{{font-size:16px;font-weight:700;color:#111827;}}
  .sec-hd .sec-note{{font-size:12px;color:var(--sub);margin-top:4px;}}
  .sec-bd{{padding:16px 22px 20px;}}
  .season-title{{font-size:44px;font-weight:800;letter-spacing:.04em;margin:14px 0 4px;background:linear-gradient(to right,#f59e0b,#ec4899,#9333ea);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;}}
  .season-sub{{font-size:13px;color:var(--sub);margin-bottom:16px;}}
  .stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;padding:18px 22px;}}
  .stat{{display:flex;align-items:flex-start;gap:12px;padding:10px 6px;}}
  .stat-ic{{width:38px;height:38px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;}}
  .stat-body{{min-width:0;}}
  .stat-lb{{font-size:12px;color:var(--sub);}}
  .stat-v{{font-size:21px;font-weight:800;letter-spacing:-.02em;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-variant-numeric:tabular-nums;margin-top:1px;}}
  .stat-sb{{font-size:11px;color:#9ca3af;margin-top:1px;}}
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:20px;}}
  @media(max-width:860px){{.grid2{{grid-template-columns:1fr;}}}}
  .chart{{width:100%;height:280px;}}
  .chart.sm{{height:210px;}}
  .tp-list{{padding:16px 22px 20px;}}
  .tp{{display:flex;align-items:center;gap:12px;padding:7px 0;}}
  .tp-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0;}}
  .tp-name{{width:86px;font-size:13px;color:#374151;flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
  .tp-bar{{flex:1;height:6px;border-radius:999px;background:#f3f4f6;overflow:hidden;}}
  .tp-bar div{{height:100%;border-radius:999px;}}
  .tp-cnt{{width:108px;text-align:right;font-size:13px;font-weight:600;color:#111827;font-variant-numeric:tabular-nums;}}
  .tp-cnt i{{font-style:normal;font-size:11px;color:#9ca3af;font-weight:400;margin-left:4px;}}
  .rk-list{{padding:4px 22px 12px;}}
  .rk{{display:flex;align-items:flex-start;gap:14px;padding:11px 4px;border-bottom:1px solid #f3f4f6;transition:background .15s;}}
  .rk:last-child{{border-bottom:none;}}
  .rk:hover{{background:#fafafa;}}
  .rk-no{{width:28px;text-align:center;font-family:ui-monospace,Consolas,monospace;font-size:14px;font-weight:800;font-variant-numeric:tabular-nums;padding-top:2px;}}
  .text-amber{{color:#f59e0b;}}.text-silver{{color:#9ca3af;}}.text-bronze{{color:#d97706;}}.text-muted{{color:#9ca3af;}}
  .rk-body{{flex:1;min-width:0;}}
  .rk-top{{display:flex;align-items:baseline;justify-content:space-between;gap:10px;}}
  .rk-name{{font-size:14px;font-weight:600;color:#111827;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
  .rk-val{{font-family:ui-monospace,Consolas,monospace;font-size:15px;font-weight:800;font-variant-numeric:tabular-nums;}}
  .rk-val i{{font-style:normal;font-size:11px;color:#9ca3af;font-weight:400;margin-left:3px;}}
  .rk-bar{{height:6px;border-radius:999px;background:#f3f4f6;overflow:hidden;margin-top:7px;}}
  .rk-bar div{{height:100%;border-radius:999px;transition:width .4s;}}
  .rk-pct{{width:44px;text-align:right;font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#9ca3af;font-variant-numeric:tabular-nums;padding-top:2px;}}
  .sub-t{{font-size:13px;font-weight:700;color:#312e81;margin:14px 0 6px;}}
  .quote{{font-size:13px;color:#374151;margin:6px 0;padding:9px 14px;background:#f9fafb;border-radius:10px;border-left:3px solid #c7d2fe;}}
  .quote .who{{color:var(--sub);font-size:12px;}}
  .wc-wrap{{padding:18px 22px 8px;}}
  .wcbox{{min-height:260px;position:relative;display:flex;flex-wrap:wrap;align-items:center;justify-content:center;align-content:center;gap:10px 18px;padding:20px 8px;}}
  .wcbox span{{font-weight:700;line-height:1.3;cursor:default;transition:transform .15s;display:inline-block;color:#374151;}}
  .wcbox span:hover{{transform:scale(1.15);color:#ee4567;}}
  .tbl-wrap{{overflow-x:auto;padding:0 22px 16px;}}
  table{{width:100%;border-collapse:collapse;font-size:13px;}}
  th,td{{border-bottom:1px solid #f3f4f6;padding:8px 10px;text-align:left;}}
  th{{color:var(--sub);font-weight:600;font-size:12px;}}
  td.num{{text-align:right;font-variant-numeric:tabular-nums;}}
  .metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:16px 22px 6px;}}
  @media(max-width:860px){{.metrics{{grid-template-columns:repeat(2,1fr);}}}}
  .metric{{padding:10px 12px;border-radius:12px;background:#f9fafb;}}
  .metric .v{{font-family:ui-monospace,Consolas,monospace;font-size:16px;font-weight:800;color:#ee4567;font-variant-numeric:tabular-nums;}}
  .metric .k{{font-size:11px;color:var(--sub);margin-top:2px;}}
  footer{{margin-top:30px;text-align:center;font-size:12px;color:var(--sub);line-height:2;}}
  footer .tag{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 14px;margin:2px;background:#fff;}}
</style>
</head>
<body>
<header>
  <div class="inner">
    <div class="kicker">Chat Report · {_esc(period_cn)}</div>
    <h1>{_esc(display)}</h1>
    <div class="meta">{_esc(d["begin"])} ~ {_esc(d["until"])} · {member_note}消息 {d["n_total"]:,} 条 · 发言 {d["senders"]} 人 · 生成于 {_esc(generated)}</div>
    <div class="badges"><span class="badge">数据直连只读</span><span class="badge">本报告基于 WuShuo 逆向微信协议 Skill 生成</span><span class="badge">由 PagePilot 承载</span></div>
  </div>
</header>

<div class="wrap">
<div class="layout">
<aside class="anchors">
  <div class="border-l" id="anchors"></div>
</aside>

<main class="main-content">

  <section class="card-sec" id="ov">
    <div class="sec-hd"><h2>🏠 总览 Overview</h2>
      <p class="sec-note">消息口径含系统/撤回/拍一拍；发言成员为该周期内实际发言的去重人数。</p></div>
    <div class="stat-grid">
      <div class="stat"><div class="stat-ic" style="background:#3b82f61a">💬</div><div class="stat-body"><div class="stat-lb">消息总量</div><div class="stat-v" style="color:#2563eb">{d["n_total"]:,}</div><div class="stat-sb">{_esc(d["span_days"])} 天累计</div></div></div>
      <div class="stat"><div class="stat-ic" style="background:#ec48991a">📷</div><div class="stat-body"><div class="stat-lb">图片/视频</div><div class="stat-v" style="color:#db2777">{d["media"].get("image", 0) + d["media"].get("video", 0):,}</div><div class="stat-sb">图 {d["media"].get("image", 0):,} · 视频 {d["media"].get("video", 0):,}</div></div></div>
      <div class="stat"><div class="stat-ic" style="background:#f59e0b1a">📅</div><div class="stat-body"><div class="stat-lb">最活跃星期</div><div class="stat-v" style="color:#d97706">{_esc(wk_name)}</div><div class="stat-sb">峰值小时 {d["peak_hour"]:02d}:00</div></div></div>
      <div class="stat"><div class="stat-ic" style="background:#22c55e1a">🌤️</div><div class="stat-body"><div class="stat-lb">周末占比</div><div class="stat-v" style="color:#16a34a">{d["weekend_ratio"]}%</div><div class="stat-sb">周末消息占比</div></div></div>
      <div class="stat"><div class="stat-ic" style="background:#f43f5e1a">🔥</div><div class="stat-body"><div class="stat-lb">峰值日</div><div class="stat-v" style="color:#e11d48">{_esc(d["peak_day"][0][5:] if d["peak_day"][0] else "-")}</div><div class="stat-sb">{d["peak_day"][1]:,} 条</div></div></div>
      <div class="stat"><div class="stat-ic" style="background:#6366f11a">🌙</div><div class="stat-body"><div class="stat-lb">夜猫子消息</div><div class="stat-v" style="color:#4f46e5">{d["night_total"]:,}</div><div class="stat-sb">23:00-05:00</div></div></div>
      <div class="stat"><div class="stat-ic" style="background:#f59e0b1a">⚡</div><div class="stat-body"><div class="stat-lb">日均消息</div><div class="stat-v" style="color:#d97706">{d["avg_day"]}</div><div class="stat-sb">按活跃天数</div></div></div>
      <div class="stat"><div class="stat-ic" style="background:#6b72801a">📊</div><div class="stat-body"><div class="stat-lb">参与率</div><div class="stat-v" style="color:#111827">{round(d["senders"] / max(1, d.get("members") or d["senders"]) * 100)}%</div><div class="stat-sb">发言/成员</div></div></div>
    </div>
    <div class="grid2">
      <div><div class="chart" id="c_type"></div></div>
      <div><div class="chart" id="c_mem"></div></div>
    </div>
  </section>

  <section class="card-sec" id="type">
    <div class="sec-hd"><h2>🔬 洞察 · 消息类型</h2>
      <p class="sec-note">类型按 local_type 语义分层；复合类型（引用/转发/卡片）并入「链接/复合」。</p></div>
    <div class="tp-list">
      {_type_progress(d["types"], types_total)}
    </div>
    <div class="sec-hd"><h2 style="font-size:14px;color:#312e81">文本深度画像（{ls["n"]:,} 条有效文本）</h2></div>
    <div class="metrics">
      <div class="metric"><div class="v">{ls["avg"]}</div><div class="k">平均长度（字）</div></div>
      <div class="metric"><div class="v">{ls["median"]}</div><div class="k">中位数（字）</div></div>
      <div class="metric"><div class="v">{ls["short"]}%</div><div class="k">短文占比 ≤10 字</div></div>
      <div class="metric"><div class="v">{ls["long"]}</div><div class="k">长文 ≥100 字</div></div>
    </div>
    <div class="chart sm" id="c_len"></div>
  </section>

  <section class="card-sec" id="time">
    <div class="sec-hd"><h2>⏰ 洞察 · 时间规律</h2>
      <p class="sec-note">小时/星期分布反映活跃节奏；热力图=小时×星期；夜猫子统计 23:00-05:00。</p></div>
    <div class="grid2">
      <div><div class="chart sm" id="c_hour"></div></div>
      <div><div class="chart sm" id="c_week"></div></div>
    </div>
    <div class="chart sm" id="c_hw"></div>
    <div class="sec-hd"><h2 style="font-size:14px;color:#312e81">🌙 夜猫子排行</h2></div>
    {_rank_rows([{"name": r["name"], "value": r["count"], "rate": r["count"]} for r in d["night_top"]], "条")}
  </section>

  <section class="card-sec" id="topic">
    <div class="sec-hd"><h2>💡 洞察 · 话题</h2>
      <p class="sec-note">通用话题词典多标签命中；按周演变观察热度迁移。</p></div>
    <div class="chart sm" id="c_topic"></div>
    <div class="chart" id="c_topicweek"></div>
    <div class="sec-bd">{quotes_html}</div>
  </section>

  <section class="card-sec" id="rel">
    <div class="sec-hd"><h2>🕸️ 洞察 · 互动关系</h2>
      <p class="sec-note">@ 提及（文本点名）、引用（转发/回复）与拍一拍共同构成互动网络；节点大小=互动量。</p></div>
    <div class="chart" id="c_graph"></div>
    <div class="grid2">
      <div><div class="sec-hd" style="padding:12px 22px 0"><h2 style="font-size:14px">@ 发起 TOP</h2></div>{_rank_rows([{"name": r["name"], "value": r["count"], "rate": r["count"]} for r in d["at_from"]], "次")}</div>
      <div><div class="sec-hd" style="padding:12px 22px 0"><h2 style="font-size:14px">被 @ TOP</h2></div>{_rank_rows([{"name": r["name"], "value": r["count"], "rate": r["count"]} for r in d["at_to"]], "次")}</div>
    </div>
  </section>

  <div class="season-title">🏆 {_esc(period_cn)}榜</div>
  <p class="season-sub">{_esc(period_cn)} · {_esc(d["begin"])} ~ {_esc(d["until"])} · 各榜单 TOP 由真实消息统计得出</p>

  <section class="card-sec" id="rank">
    <div class="sec-hd"><h2>🥇 总榜 · 发言排行</h2><p class="sec-note">按有效发言条数统计，占比=条数/总消息。</p></div>
    {_rank_rows([{"name": r["name"], "value": r["count"], "rate": r["rate"], "days": r["days"]} for r in d["sender_rank"]], "条")}
  </section>

  <section class="card-sec" id="prox">
    <div class="sec-hd"><h2>🤝 邻近度排行</h2>
      <p class="sec-note">互动亲密度 = @×1 + 引用×2 + 拍一拍×1.5；矩阵热力图 = 前 20 名成员两两互动。</p></div>
    <div class="chart" id="c_inti"></div>
    {_rank_rows([{"name": f'{r["from"]} ↔ {r["to"]}', "value": r["score"], "rate": r["score"]} for r in d["intimacy_top"]], "分")}
  </section>

  <section class="card-sec" id="dup">
    <div class="sec-hd"><h2>🔁 最火复读</h2><p class="sec-note">≥4 字且被重复 5 次以上的句子。</p></div>
    {_rank_rows([{"name": r["text"], "value": r["count"], "rate": r["count"]} for r in d["dup_top"]], "次")}
  </section>

  <section class="card-sec" id="catch">
    <div class="sec-hd"><h2>🗣️ 口头禅 & 含笑量</h2>
      <p class="sec-note">口头禅=个人最高频 2-6 字词；含笑量统计 哈哈/233/😂 等笑声表达。</p></div>
    <div class="grid2">
      <div><div class="sec-hd" style="padding:12px 22px 0"><h2 style="font-size:14px">口头禅 TOP</h2></div>{_rank_rows([{"name": f'{r["name"]} · {r["word"]}', "value": r["count"], "rate": r["count"]} for r in d["catch_top"]], "次")}</div>
      <div><div class="sec-hd" style="padding:12px 22px 0"><h2 style="font-size:14px">含笑量 TOP（{d["laugh_total"]} 次）</h2></div>{_rank_rows([{"name": r["name"], "value": r["count"], "rate": r["rate"]} for r in d["laugh_top"]], "次")}</div>
    </div>
  </section>

  <section class="card-sec" id="kw">
    <div class="sec-hd"><h2>☁️ 关键词</h2>
      <p class="sec-note">文本 2-6 字词频（去停用词）；字号与颜色映射频次，点击词条无交互仅为排版。</p></div>
    <div class="wc-wrap"><div class="wcbox" id="c_wc">{kw_spans}</div></div>
    {_table(["排名", "关键词", "次数"], [[i + 1, w["word"], f'{w["count"]:,}'] for i, w in enumerate(d["kw_top"][:20])])}
  </section>

  <footer>
    <div class="tag">本报告基于 WuShuo 逆向微信协议 Skill 生成</div>
    <div class="tag">由 PagePilot 承载</div>
    <div style="margin-top:10px">© 2026 WuShuo · 本地数据只读分析 · 不涉及任何第三方服务</div>
  </footer>

</main>
</div>
</div>

<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<script>
(function(){{
  var CHART={js};
  var COLORS=['#6366f1','#8b5cf6','#ec4899','#f43f5e','#f97316','#eab308','#22c55e','#14b8a6','#06b6d4','#3b82f6'];
  var TPA={{trigger:'axis',triggerOn:'click',renderMode:'richText',confine:true,backgroundColor:'rgba(17,24,39,.92)',borderColor:'transparent',textStyle:{{color:'#fff',fontSize:11}},padding:[6,10]}};
  var TPI={{trigger:'item',triggerOn:'click',renderMode:'richText',confine:true,backgroundColor:'rgba(17,24,39,.92)',borderColor:'transparent',textStyle:{{color:'#fff',fontSize:11}},padding:[6,10]}};
  var GRAD={{type:'linear',x:0,y:0,x2:0,y2:1,colorStops:[{{offset:0,color:'#ee4567'}},{{offset:1,color:'#f7758c'}}]}};

  function mk(el){{return echarts.init(document.getElementById(el));}}
  function pie(id,title,data){{
    var c=mk(id);
    c.setOption({{backgroundColor:'transparent',color:COLORS,
      title:{{text:title,left:'center',textStyle:{{color:'#111827',fontSize:15,fontWeight:700}}}},
      tooltip:TPI,legend:{{type:'scroll',orient:'vertical',right:10,top:30,bottom:10,textStyle:{{fontSize:11,color:'#6b7280',overflow:'truncate',width:110}}}},
      series:[{{type:'pie',radius:['48%','70%'],center:['36%','52%'],avoidLabelOverlap:true,
        itemStyle:{{borderRadius:5,borderColor:'#fff',borderWidth:2}},
        label:{{show:false}},data:data}}]}});
  }}
  function bar(id,title,xdata,data,rot){{
    var c=mk(id);
    c.setOption({{backgroundColor:'transparent',
      title:{{text:title,left:'center',textStyle:{{color:'#111827',fontSize:15,fontWeight:700}}}},
      tooltip:TPA,grid:{{left:40,right:18,top:44,bottom:rot?58:32,containLabel:true}},
      xAxis:{{type:'category',data:xdata,axisLine:{{show:false}},axisTick:{{show:false}},axisLabel:{{fontSize:10,color:'#6b7280',rotate:rot||0,hideOverlap:true}}}},
      yAxis:{{type:'value',axisLine:{{show:false}},axisTick:{{show:false}},splitLine:{{lineStyle:{{type:'dashed',color:'#e5e7eb'}}}},axisLabel:{{fontSize:10,color:'#6b7280'}}}},
      series:[{{type:'bar',data:data,itemStyle:{{color:GRAD,borderRadius:5}},barMaxWidth:34}}]}});
  }}
  function heat(id,title,xdata,ydata,data,maxv){{
    var c=mk(id);
    c.setOption({{backgroundColor:'transparent',
      title:{{text:title,left:'center',textStyle:{{color:'#111827',fontSize:15,fontWeight:700}}}},
      tooltip:{{trigger:'item',triggerOn:'click',renderMode:'richText',confine:true,backgroundColor:'rgba(17,24,39,.92)',textStyle:{{color:'#fff',fontSize:11}}}},
      grid:{{left:52,right:14,top:46,bottom:34,containLabel:true}},
      xAxis:{{type:'category',data:xdata,axisLine:{{show:false}},axisTick:{{show:false}},axisLabel:{{fontSize:10,color:'#6b7280'}}}},
      yAxis:{{type:'category',data:ydata,axisLine:{{show:false}},axisTick:{{show:false}},axisLabel:{{fontSize:10,color:'#6b7280'}}}},
      visualMap:{{min:0,max:maxv,calculable:false,orient:'horizontal',left:'center',bottom:0,itemWidth:120,itemHeight:10,
        inRange:{{color:['#fce4ec','#f8a4b8','#f06292','#e91e63']}},textStyle:{{fontSize:9}}}},
      series:[{{type:'heatmap',data:data,itemStyle:{{borderColor:'#fff',borderWidth:2,borderRadius:3}},
        label:{{show:false}},emphasis:{{itemStyle:{{shadowBlur:8,shadowColor:'rgba(0,0,0,.3)'}}}}}}]}});
  }}

  pie('c_type','消息类型占比',CHART.types);
  pie('c_mem','成员水群分布',CHART.mem);
  bar('c_len','文本长度分位（字）',['P25','P50 中位','P75','P90','最长'],CHART.len);
  bar('c_hour','24 小时活跃分布（条）',CHART.hours_lbl,CHART.hours);
  bar('c_week','星期分布（条）',CHART.week_cn,CHART.week);
  var hwData=[];
  for(var hi=0;hi<24;hi++){{
    for(var wj=0;wj<7;wj++){{hwData.push([wj,hi,CHART.hw[hi][wj]]);}}
  }}
  heat('c_hw','活跃热力图（小时 × 星期）',CHART.week_cn,CHART.hours_lbl,hwData,Math.max.apply(null,CHART.hw.map(function(r){{return Math.max.apply(null,r);}}))||1);
  bar('c_topic','话题命中条数',CHART.topics.map(function(x){{return x.name;}}),CHART.topics.map(function(x){{return x.value;}}),28);

  var ctw=mk('c_topicweek');
  ctw.setOption({{backgroundColor:'transparent',
    title:{{text:'话题每周演变（条）',left:'center',textStyle:{{color:'#111827',fontSize:15,fontWeight:700}}}},
    tooltip:TPA,legend:{{top:34,type:'scroll',itemWidth:12,itemHeight:8,textStyle:{{fontSize:10,color:'#6b7280'}}}},
    grid:{{left:44,right:18,top:78,bottom:30,containLabel:true}},
    xAxis:{{type:'category',data:CHART.weeks,axisLine:{{show:false}},axisTick:{{show:false}},axisLabel:{{fontSize:10,color:'#6b7280'}}}},
    yAxis:{{type:'value',axisLine:{{show:false}},axisTick:{{show:false}},splitLine:{{lineStyle:{{type:'dashed',color:'#e5e7eb'}}}},axisLabel:{{fontSize:10,color:'#6b7280'}}}},
    series:CHART.week_series}});

  var cg=mk('c_graph');
  cg.setOption({{backgroundColor:'transparent',
    title:{{text:'互动关系网络（@ 提及）',left:'center',textStyle:{{color:'#111827',fontSize:15,fontWeight:700}}}},
    tooltip:TPI,series:[{{type:'graph',layout:'force',roam:true,
      label:{{show:true,fontSize:10,color:'#374151'}},
      force:{{repulsion:260,edgeLength:[50,110],gravity:.08}},
      lineStyle:{{color:'#a5b4fc',width:1.2,opacity:.55,curveness:.12}},
      emphasis:{{focus:'adjacency',lineStyle:{{width:3}}}},
      data:CHART.graph.nodes.map(function(n){{
        var r=14+Math.min(10,Math.sqrt(n.value)*1.8);
        return {{name:n.name,value:n.value,symbolSize:r,itemStyle:{{color:n.value>=25?'#f43f5e':(n.value>=15?'#f59e0b':'#6366f1')}},label:{{fontWeight:600}}}};
      }}),
      links:CHART.graph.links}}]}});

  var im=mk('c_inti');
  var imData=[];
  for(var mi=0;mi<CHART.inti_members.length;mi++){{
    for(var mj=0;mj<CHART.inti_members.length;mj++){{
      imData.push([mj,mi,CHART.inti_matrix[mi][mj]]);
    }}
  }}
  heat('c_inti','邻近度矩阵（前 20 成员）',CHART.inti_members,CHART.inti_members,imData,
    Math.max.apply(null,CHART.inti_matrix.map(function(r){{return Math.max.apply(null,r);}}))||1);

  // 词云：纯 CSS 错落排布
  var spans=document.querySelectorAll('#c_wc span');
  var maxC=spans.length?parseInt(spans[0].title.split('×')[1]||'1',10):1;
  for(var si=0;si<spans.length;si++){{
    var cnt=parseInt(spans[si].title.split('×')[1]||'1',10);
    var f=15+Math.round(cnt/maxC*30);
    spans[si].style.fontSize=f+'px';
    spans[si].style.color=COLORS[si%COLORS.length];
    spans[si].style.transform='rotate('+((si%5)-2)*3+'deg)';
  }}

  // 右侧锚点高亮
  var secs=['ov','type','time','topic','rel','rank','prox','dup','catch','kw'];
  var labels=['🏠 总览','🔬 类型','⏰ 时间','💡 话题','🕸️ 关系','🥇 总榜','🤝 邻近度','🔁 复读','🗣️ 口头禅','☁️ 关键词'];
  var ab=document.getElementById('anchors');
  for(var ai=0;ai<secs.length;ai++){{
    var a=document.createElement('a');
    a.href='#'+secs[ai];a.textContent=labels[ai];a.dataset.sec=secs[ai];
    a.addEventListener('click',function(){{
      var cur=this;
      setTimeout(function(){{var sels=ab.querySelectorAll('a');for(var k=0;k<sels.length;k++){{sels[k].className=(sels[k]===cur)?'on':'';}}}},10);
    }});
    ab.appendChild(a);
  }}
  window.addEventListener('scroll',function(){{
    var pos=window.scrollY+120,cur='ov';
    for(var si2=0;si2<secs.length;si2++){{
      var el=document.getElementById(secs[si2]);
      if(el&&el.offsetTop<=pos){{cur=secs[si2];}}
    }}
    var sels2=ab.querySelectorAll('a');
    for(var k2=0;k2<sels2.length;k2++){{sels2[k2].className=(sels2[k2].dataset.sec===cur)?'on':'';}}
  }});
}})();
</script>
</body>
</html>"""
    return html


# ---------- CLI ----------
PERIODS = {"daily", "weekly", "monthly", "custom"}


def resolve_period(period: str, since_s: str | None, until_s: str | None):
    """返回 (begin, until) 两个 datetime。"""
    now = datetime.now()
    if period == "daily":
        begin = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return begin, now
    if period == "weekly":
        return now - timedelta(days=6), now
    if period == "monthly":
        return now - timedelta(days=29), now
    # custom
    until = datetime.strptime(until_s, "%Y-%m-%d") if until_s else now
    since = datetime.strptime(since_s, "%Y-%m-%d") if since_s else now - timedelta(days=29)
    return since, until


def main():
    ap = argparse.ArgumentParser(description="聊天记录深度分析（群/私聊通用，只读直连加密库）")
    ap.add_argument("--db-dir", required=True, help="db_storage 目录")
    ap.add_argument("--key", help="64位hex密钥（所有库共用）")
    ap.add_argument("--keys", help="all_keys.json 路径（每库独立密钥）")
    ap.add_argument("--session", help="会话 username 或 群名/昵称；多会话用 --sessions")
    ap.add_argument("--sessions", help="逗号分隔的多个会话（群名/昵称/username）批量生成")
    ap.add_argument("--period", choices=sorted(PERIODS), default="custom",
                    help="周期模式：daily=今天 / weekly=近7天 / monthly=近30天 / custom=--since/--until（默认）")
    ap.add_argument("--since", help="起始日期 YYYY-MM-DD（custom 模式）")
    ap.add_argument("--until", help="结束日期 YYYY-MM-DD（custom 模式）")
    ap.add_argument("--out", help="输出 HTML 路径（默认 <显示名>_<周期>_<日期>.html）")
    ap.add_argument("--out-dir", help="输出目录（自动命名）")
    ap.add_argument("--top", type=int, default=10, help="榜单 TOP N（默认 10）")
    ap.add_argument("--json", action="store_true", help="额外输出分析 JSON（同前缀 .json）")
    args = ap.parse_args()

    if not args.session and not args.sessions:
        print("[!] 请提供 --session 或 --sessions", file=sys.stderr)
        sys.exit(2)
    if args.period == "custom" and not args.since and not args.until:
        args.since = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
        args.until = datetime.now().strftime("%Y-%m-%d")

    sessions = [s.strip() for s in (args.sessions or args.session).split(",") if s.strip()]
    begin, until = resolve_period(args.period, args.since, args.until)
    begin_ts = int(begin.timestamp())
    end_ts = int((until + timedelta(days=1)).timestamp()) if args.period != "daily" else int(until.timestamp()) + 1

    az = ChatAnalyzer(args.db_dir, keys_file=args.keys, enc_key=args.key)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    for sess in sessions:
        username, display = az.resolve_session(sess)
        data = az.scan(username, begin_ts, end_ts, top=args.top)
        data["members"] = az.group_member_count(username)
        data["begin"] = begin.strftime("%Y-%m-%d")
        data["until"] = until.strftime("%Y-%m-%d")
        data["period"] = args.period

        if args.out and len(sessions) == 1:
            out = args.out
        elif args.out_dir:
            os.makedirs(args.out_dir, exist_ok=True)
            out = os.path.join(args.out_dir, f"{display}_{args.period}_{until.strftime('%Y%m%d')}.html")
        else:
            out = f"{display}_{args.period}_{until.strftime('%Y%m%d')}.html"

        html = build_html(data, display, args.top, args.period, generated)
        with io.open(out, "w", encoding="utf-8") as f:
            f.write(html)

        if args.json:
            json_path = out.rsplit(".", 1)[0] + ".json"
            with io.open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)

        members_note = f" · 群成员 {data['members']} 人" if data.get("members") else ""
        print(f"HTML 报告: {out}")
        print(f"会话: {display} ({username}){members_note}")
        print(f"周期[{args.period}]: {data['begin']} ~ {data['until']} · 消息 {data['n_total']:,} 条 · "
              f"发言 {data['senders']} 人 · 文本 {data['n_text']:,} 条")
        print(f"峰值日: {data['peak_day'][0]} ({data['peak_day'][1]:,} 条) · "
              f"话题命中 TOP: {list(data['topic_cnt'].items())[:3]}")


if __name__ == "__main__":
    main()
