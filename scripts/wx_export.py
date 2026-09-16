#!/usr/bin/env python3
"""微信群聊导出 — 一键流水线（自动探测 + 缓存复用）

用法:
    # 常用：导出指定群（全自动：探测目录 -> 提密钥 -> 解密 -> 导出）
    python wx_export.py --group "某时间管理群【星期二】"

    # 列出所有群（用于确认群名）
    python wx_export.py --list-groups

    # 指定输出路径 / 缓存目录 / 手动指定数据目录
    # （缓存默认 ~/.wxcache；导出位置必须显式指定 --outdir 或 --out）
    python wx_export.py --group "群名" --outdir "D:\\导出"
    python wx_export.py --group "群名" --cache "D:\\tools\\.wxcache" --outdir "D:\\导出"
    python wx_export.py --group "群名" --db-dir "D:\\微信数据\\...\\db_storage"

    # 清掉缓存（密钥 + 解密库，敏感）
    python wx_export.py --purge

    # v2.5: 全天跨会话梳理包（群+私聊，一天一梳：总览+逐会话文件+小时分节）
    python wx_export.py --digest yesterday --outdir "D:\\导出\\昨天"
    python wx_export.py --digest 2026-09-16 --outdir "D:\\导出" --merge-under 100

设计要点:
    - 缓存复用：密钥/解密库缓存在 --cache 下，第二次导出别的群只需几秒（跳过耗时的提密钥+解密）。
    - 自动探测：从运行中的 Weixin.exe 定位安装目录 -> xwechat/config/*.ini -> 数据根目录。
      绿色版配置在安装目录旁，不在 AppData（技能文档旧说法有误）。
    - 安全消歧：群名匹配到多个候选时不瞎猜，列出候选并退出。
    - zstd：检测到 zstandard 就自动开启解压（实测约 80% 消息是压缩的，属刚需非可选）。
"""
import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

# 群名常含 emoji；stdout 重定向到管道/文件时 Python 退回 GBK，替换而非崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
# 密钥提取/解密脚本按平台切换：Windows 走 413 + wcdb_key_tool_windows；
# darwin/linux 走各自的 extract_keys_<os>.py（CLI 同为 extract/decrypt 子命令）。
if sys.platform == "win32":
    EXTRACT = os.path.join(HERE, "extract_keys_413.py")
    DECRYPT = os.path.join(HERE, "wcdb_key_tool_windows.py")
elif sys.platform == "darwin":
    EXTRACT = os.path.join(HERE, "extract_keys_macos.py")
    DECRYPT = os.path.join(HERE, "extract_keys_macos.py")
elif sys.platform.startswith("linux"):
    EXTRACT = os.path.join(HERE, "extract_keys_linux.py")
    DECRYPT = os.path.join(HERE, "extract_keys_linux.py")
else:
    sys.exit(f"[x] 不支持的平台: {sys.platform!r}（仅 win32/darwin/linux）")
EXPORT = os.path.join(HERE, "export_group_md.py")
EXTRACT_IMG = os.path.join(HERE, "extract_image_key.py")
EXPORT_MEDIA = os.path.join(HERE, "export_media.py")
EXPORT_SNS = os.path.join(HERE, "export_sns.py")
EXPORT_FAVORITE = os.path.join(HERE, "export_favorite.py")
EXPORT_BIZ = os.path.join(HERE, "export_biz.py")
EXPORT_TRANSFER = os.path.join(HERE, "export_transfer.py")
SEARCH = os.path.join(HERE, "search_messages.py")
EXPORT_INCREMENTAL = os.path.join(HERE, "export_incremental.py")
EXPORT_DAY_DIGEST = os.path.join(HERE, "export_day_digest.py")
EXPORT_ALL_SESSIONS = os.path.join(HERE, "export_all_sessions.py")
WATCH_MESSAGES = os.path.join(HERE, "watch_messages.py")

# 缓存默认位置（敏感：含解密库+密钥），用 --cache 可改。
# 发布版默认用户主目录（跨机器恒存在）；作者/团队可用 --cache 指向私有缓存。
DEFAULT_CACHE = os.path.expanduser(r"~\.wxcache")
# 导出目录：发布版不做默认值 —— 必须显式指定 --outdir/--out。
# ⚠️ 不要设 os.getcwd()：若在 scripts/ 下运行，会把个人聊天记录写进技能目录
#    （数据灌进技能 = 违反"技能与数据分离"，且发布时会误提交）。
DEFAULT_OUTDIR = None

# ---------------------------------------------------------------- 工具


def log(msg=""):
    print(msg, flush=True)


def step(n, msg):
    log(f"\n{'='*60}\n[Step {n}] {msg}\n{'='*60}")


def run(cmd):
    """流式执行子进程（输出直接打到控制台，便于观察长任务进度）"""
    r = subprocess.run([sys.executable] + cmd)
    if r.returncode != 0:
        sys.exit(f"[x] 命令失败 exit={r.returncode}: {' '.join(cmd[:2])}")


def find_wechat_install_dirs():
    """微信安装目录候选（按平台分支）。
    - win32：运行中 Weixin.exe 所在目录（原逻辑，保持现状）。
    - darwin：/Applications/WeChat.app。
    - linux：/opt/wechat、/usr/bin/wechat 同级目录。
    """
    if sys.platform == "darwin":
        app = "/Applications/WeChat.app"
        return [app] if os.path.isdir(app) else []
    if sys.platform.startswith("linux"):
        dirs = []
        for c in ("/opt/wechat", os.path.dirname("/usr/bin/wechat")):
            if os.path.isdir(c):
                dirs.append(c)
        return dirs
    # win32（原逻辑，逐字节保持）
    dirs = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Name Weixin -ErrorAction SilentlyContinue | "
             "Select-Object -First 1 -ExpandProperty Path)"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if out and os.path.isfile(out):
            dirs.append(os.path.dirname(out))
    except Exception:
        pass
    return dirs


def run_extract_keys(db_dir, dump, keys):
    """按平台调用对应密钥提取脚本（Windows 走 413 直参；mac/linux 走 extract 子命令）。"""
    if sys.platform == "win32":
        run([EXTRACT, "--db-dir", db_dir, "--dump-dir", dump, "--out", keys])
    else:
        # darwin/linux：python <script> extract --db-dir ... --out ...
        run([EXTRACT, "extract", "--db-dir", db_dir, "--out", keys])


def read_text(path):
    with open(path, "rb") as f:
        b = f.read()
    for enc in ("utf-8", "utf-16-le", "gbk"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", errors="replace")


def db_file_set(d):
    """库文件相对路径集合（按 / 归一）。用于精确比对源库与解密库的覆盖面：
    数量比例法有盲区 —— 微信新增分库后 23/24 仍满足 0.8 阈值，新库会静默漏解（审计 E3）"""
    return {os.path.relpath(p, d).replace("\\", "/")
            for p in glob.glob(os.path.join(d, "**", "*.db"), recursive=True)}


def _detect_db_dir_win():
    """Windows：ini -> 数据根目录 -> xwechat_files/<wxid>/db_storage（原逻辑，逐字节保持）。"""
    ini_dirs = []
    for d in find_wechat_install_dirs():
        ini_dirs.append(os.path.join(d, "xwechat", "config"))
    for env in ("APPDATA", "LOCALAPPDATA"):
        v = os.environ.get(env)
        if v:
            ini_dirs.append(os.path.join(v, "Tencent", "xwechat", "config"))

    roots = []
    for idir in ini_dirs:
        for ini in glob.glob(os.path.join(idir, "*.ini")):
            try:
                t = read_text(ini)
            except Exception:
                continue
            # 逐行解析，不用正则匹配反斜杠（raw string 里 \\ 易写错成匹配两个反斜杠）
            for ln in t.splitlines():
                s = ln.strip()
                if "=" in s:                      # 兼容 key=value
                    s = s.split("=", 1)[1].strip()
                s = s.strip('"').strip("'").rstrip("\\")
                if len(s) > 2 and s[1] == ":" and os.path.isdir(s) \
                        and os.path.isdir(os.path.join(s, "xwechat_files")):
                    roots.append((s, ini))
    if not roots:
        return None, f"未在 ini 中找到数据根目录（已查: {ini_dirs}）"

    root, ini = roots[0]
    log(f"  [i] 数据根目录: {root}   (来源 {os.path.basename(ini)})")
    cands = sorted(glob.glob(os.path.join(root, "xwechat_files", "*", "db_storage")))
    cands = [c for c in cands if os.path.isdir(c)]
    if not cands:
        return None, f"{root} 下没有 xwechat_files/*/db_storage"
    if len(cands) > 1:
        # 多个账号：取 .db 文件最多的那个
        cands.sort(key=lambda c: -len(glob.glob(os.path.join(c, "**", "*.db"), recursive=True)))
        log(f"  [i] 发现 {len(cands)} 个账号目录，取消息库最多的: {os.path.basename(os.path.dirname(cands[0]))}")
    return cands[0], None


def _pick_account(cands):
    """多账号候选取 .db 最多的那个（三平台共用）。"""
    cands = [c for c in cands if os.path.isdir(c)]
    if not cands:
        return None
    if len(cands) > 1:
        cands.sort(key=lambda c: -len(glob.glob(os.path.join(c, "**", "*.db"), recursive=True)))
        log(f"  [i] 发现 {len(cands)} 个账号目录，取消息库最多的: {os.path.basename(os.path.dirname(cands[0]))}")
    return cands[0]


def detect_db_dir():
    """自动探测 db_storage（按 sys.platform 三分支）。

    - win32：ini -> 数据根目录 -> xwechat_files/<wxid>/db_storage（原逻辑）。
    - darwin：~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage。
    - linux：~/.local/share/com.tencent.wechat/xwechat_files/<wxid>/db_storage。
    """
    if sys.platform == "darwin":
        root = os.path.expanduser(
            "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")
        if not os.path.isdir(root):
            return None, f"未找到 mac 沙盒数据目录: {root}"
        cands = sorted(glob.glob(os.path.join(root, "*", "db_storage")))
        db = _pick_account(cands)
        return (db, None) if db else (None, f"{root} 下没有 */db_storage")

    if sys.platform.startswith("linux"):
        root = os.path.expanduser("~/.local/share/com.tencent.wechat/xwechat_files")
        if not os.path.isdir(root):
            return None, f"未找到 Linux 数据目录: {root}（兜底: ~/.xwechat）"
        cands = sorted(glob.glob(os.path.join(root, "*", "db_storage")))
        db = _pick_account(cands)
        return (db, None) if db else (None, f"{root} 下没有 */db_storage")

    return _detect_db_dir_win()


def db_count(d):
    return len(glob.glob(os.path.join(d, "**", "*.db"), recursive=True))


# ---------------------------------------------------------------- 群解析


def list_groups(dec):
    cdb = None
    for root, _d, files in os.walk(dec):
        if "contact.db" in files:
            cdb = os.path.join(root, "contact.db")
            break
    if not cdb:
        sys.exit("[x] 找不到 contact.db")
    conn = sqlite3.connect(cdb)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT username, nick_name, remark FROM contact WHERE username LIKE '%@chatroom%'"
    ).fetchall()
    conn.close()
    return rows


def resolve_group(dec, keyword):
    rows = list_groups(dec)
    kw = keyword.strip()
    exact = [r for r in rows if (r["nick_name"] or "") == kw or (r["remark"] or "") == kw]
    if len(exact) == 1:
        return exact[0]
    # 多个精确同名/多个片段命中 -> 都不猜，列出候选退出（由下面的 cands 分支统一处理）
    cands = [r for r in rows if kw in (r["nick_name"] or "") or kw in (r["remark"] or "")]
    if not cands:
        sys.exit(f"[x] 没有昵称/备注包含「{kw}」的群。用 --list-groups 看看有哪些群。")
    if len(cands) > 1:
        log(f"[!] 「{kw}」匹配到 {len(cands)} 个群，请确认后用更完整的群名重跑：")
        for r in cands:
            log(f"    - {r['nick_name']}   (备注: {r['remark'] or '-'})  {r['username']}")
        sys.exit(1)
    return cands[0]

def list_contacts(dec, with_groups=False):
    """联系人列表（默认只列私聊对象；with_groups=True 时含群）。"""
    cdb = None
    for root, _d, files in os.walk(dec):
        if "contact.db" in files:
            cdb = os.path.join(root, "contact.db")
            break
    if not cdb:
        sys.exit("[x] 找不到 contact.db")
    conn = sqlite3.connect(cdb)
    conn.row_factory = sqlite3.Row
    if with_groups:
        rows = conn.execute(
            "SELECT username, nick_name, remark FROM contact WHERE username != ''"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT username, nick_name, remark FROM contact "
            "WHERE username != '' AND username NOT LIKE '%@chatroom' AND username NOT LIKE '%@openim'"
        ).fetchall()
    conn.close()
    return rows


def resolve_contact(dec, keyword):
    """按昵称/备注定位私聊联系人（绝不把群当私聊；群名撞上时明确排除）。"""
    rows = list_contacts(dec)
    kw = keyword.strip()
    exact = [r for r in rows if (r["remark"] or "") == kw or (r["nick_name"] or "") == kw]
    if len(exact) == 1:
        return exact[0]
    cands = [r for r in rows if kw in (r["remark"] or "") or kw in (r["nick_name"] or "")]
    if not cands:
        sys.exit(f"[x] 没有昵称/备注包含「{kw}」的联系人。用 --list-contacts 看看有哪些联系人。")
    if len(cands) > 1:
        log(f"[!] 「{kw}」匹配到 {len(cands)} 个联系人，请确认后用更完整的名称重跑：")
        for r in cands:
            nm = r["remark"] or r["nick_name"] or r["username"]
            log(f"    - {nm}   (username: {r['username']})")
        sys.exit(1)
    return cands[0]


# ---------------------------------------------------------------- 主流程


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="群名（支持片段，唯一匹配即可）")
    ap.add_argument("--user", help="私聊联系人昵称/备注（支持片段，唯一匹配即可）")
    ap.add_argument("--media", metavar="会话名|all",
                    help="导出媒体（图片解密+WXGF转码；指定会话名或 all 全部）")
    ap.add_argument("--media-video", action="store_true",
                    help="配合 --media：同时复制视频（明文 mp4 直拷）")
    ap.add_argument("--out", help="输出 Markdown 路径（默认 <outdir>/<群名>_聊天记录.md）")
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR,
                    help="输出目录（不设 --out 时导出文件放这里；必填，默认无）")
    ap.add_argument("--cache", default=DEFAULT_CACHE,
                    help="缓存目录（密钥+解密库，敏感；默认 ~/.wxcache）")
    ap.add_argument("--db-dir", help="手动指定 db_storage（跳过自动探测）")
    ap.add_argument("--list-groups", action="store_true", help="只列出所有群名后退出")
    ap.add_argument("--list-contacts", action="store_true", help="只列出所有联系人后退出")
    ap.add_argument("--purge", action="store_true", help="删除缓存（密钥+解密库，敏感；需配 --yes 确认）")
    ap.add_argument("--yes", action="store_true", help="配合 --purge 跳过删除确认")
    ap.add_argument("--sqlite", help="结构化输出 SQLite 路径（统计底座，可选）")
    ap.add_argument("--sns", action="store_true", help="v2.3: 导出朋友圈（只读已解密缓存，--outdir 必填）")
    ap.add_argument("--favorite", action="store_true", help="v2.3: 导出收藏（只读已解密缓存，--outdir 必填）")
    ap.add_argument("--biz", action="store_true", help="v2.3: 导出服务号/公众号文章（--outdir 必填）")
    ap.add_argument("--transfer", nargs="?", const="all", metavar="kind",
                    help="v2.3: 导出转账/红包/小程序（kind=transfer/redpacket/miniapp/all，默认 all；--outdir 必填）")
    ap.add_argument("--search", metavar="关键词", help="v2.3: 聊天全文搜索（结果打到 --outdir/搜索结果.md）")
    ap.add_argument("--incremental", metavar="会话名",
                    help="v2.3: 增量导出指定会话（状态文件记录上次位置；--outdir 必填）")
    ap.add_argument("--digest", metavar="YYYY-MM-DD|yesterday",
                    help="v2.5: 全天跨会话导出（群+私聊，一天一梳；--outdir 必填；"
                         "日期或 today/今天、yesterday/昨天）")
    ap.add_argument("--cap", type=int, default=None,
                    help="配合 --digest：每条消息截断长度（默认 150 字）")
    ap.add_argument("--merge-under", type=int, default=None, metavar="N",
                    help="配合 --digest：少于 N 条的会话并入 _其余会话合集.md（默认 0=关闭）")
    ap.add_argument("--all-sessions", action="store_true",
                    help="v2.4: 跨全部会话(群+私聊)按时间批量导出汇总（--outdir 必填，"
                         "透传 --last/--since/--until；输出<outdir>/全部会话汇总.md + 可选底座）")
    ap.add_argument("--watch", metavar="会话名",
                    help="监听指定群/联系人新消息（只读解密缓存；--outdir 必填）")
    ap.add_argument("--watch-all", action="store_true",
                    help="监听全部已知会话新消息，并自动发现新分片（--outdir 必填）")
    ap.add_argument("--watch-once", action="store_true",
                    help="配合 --watch/--watch-all：只轮询一轮后退出")
    ap.add_argument("--watch-since",
                    help="配合监听：首次运行从 Unix 秒或 ISO 日期时间回放")
    ap.add_argument("--watch-format", choices=("jsonl", "text"), default="jsonl",
                    help="配合监听：输出格式（默认 jsonl）")
    ap.add_argument("--watch-interval", type=float, default=1.0,
                    help="配合监听：轮询间隔秒数（默认 1）")
    ap.add_argument("--since", help="起始时间 YYYY-MM-DD[ HH:MM:SS]（含）")
    ap.add_argument("--until", help="结束时间 YYYY-MM-DD[ HH:MM:SS]（含当天）")
    ap.add_argument("--last", help="时间范围：today/今天、yesterday/昨天、7d/近7天、2w、3m、1y、all/全部")
    args = ap.parse_args()

    if args.purge:
        # 删的是密钥+明文解密库（敏感，重建约 6min），必须显式 --yes 防手滑
        if not args.yes:
            sys.exit("[x] --purge 将删除缓存里的密钥与解密库（敏感）。确认请用: wx_export.py --purge --yes")
        if os.path.isdir(args.cache):
            shutil.rmtree(args.cache)
            log(f"[√] 已删除缓存: {args.cache}")
        else:
            log(f"[i] 缓存不存在: {args.cache}")
        return

    # 动作互斥：群 / 私聊 / 媒体 / 列表 / v2.3 新模块 必须选一个且互不混用
    actions = [a for a, on in (("--group", args.group), ("--user", args.user),
                               ("--media", args.media), ("--list-groups", args.list_groups),
                               ("--list-contacts", args.list_contacts),
                               ("--sns", args.sns), ("--favorite", args.favorite),
                               ("--biz", args.biz), ("--transfer", args.transfer),
                               ("--search", args.search), ("--incremental", args.incremental),
                               ("--digest", args.digest),
                               ("--all-sessions", args.all_sessions),
                               ("--watch", args.watch), ("--watch-all", args.watch_all)) if on]
    if not actions:
        sys.exit("[x] 需要指定动作之一：--group / --user / --media / --list-groups / --list-contacts / "
                 "--sns / --favorite / --biz / --transfer / --search / --incremental / "
                 "--digest / --all-sessions / --watch / --watch-all")
    if len(actions) > 1:
        sys.exit(f"[x] 动作互斥，一次只做一个：{', '.join(actions)}")

    # 未指定输出位置时起步即拒（不再白跑 6 分钟内存扫描后才拒绝）
    needs_out = args.group or args.user
    if needs_out and not args.out and not args.outdir:
        sys.exit("[x] 未指定导出位置：请用 --outdir 指定输出目录，或用 --out 指定文件路径。\n"
                 "    例: wx_export.py --group \"群名\" --outdir \"D:\\导出\"")
    if args.media and not args.outdir:
        sys.exit("[x] --media 需要 --outdir 指定媒体输出目录。\n"
                 "    例: wx_export.py --media all --outdir \"D:\\媒体导出\"")
    if (args.watch or args.watch_all) and not args.outdir:
        sys.exit("[x] --watch 需要 --outdir 指定监听输出目录。\n"
                 "    例: wx_export.py --watch \"群名\" --outdir \"D:\\监听\"")
    if args.watch_interval < 0:
        sys.exit("[x] --watch-interval 不能为负数")
    if args.watch_once and not (args.watch or args.watch_all):
        sys.exit("[x] --watch-once 只能配合 --watch 或 --watch-all")

    # 启动预检（人人可用）：缓存/输出目录所在盘符不存在时（os.makedirs 抛 WinError 3），
    # 把"中途崩"变成"起步时给清晰指引"。
    for label, pp in (("--cache", args.cache),
                      ("--outdir", args.outdir),
                      ("--out", args.out)):
        if not pp:
            continue
        drive = os.path.splitdrive(os.path.abspath(pp))[0]
        if drive and not os.path.isdir(drive + os.sep):
            sys.exit(f"[x] {label} 所在盘符不存在: {pp}\n"
                     f"    请显式指定，例如: wx_export.py --group \"群名\" {label} D:\\某目录")

    keys = os.path.join(args.cache, "all_keys.json")
    dump = os.path.join(args.cache, "config_dump")
    dec = os.path.join(args.cache, "decrypted")
    os.makedirs(args.cache, exist_ok=True)

    # ---- Step 0 定位数据目录
    step(0, "定位微信数据目录")
    if args.db_dir:
        db_dir = args.db_dir
        log(f"  [i] 手动指定: {db_dir}")
    else:
        db_dir, err = detect_db_dir()
        if not db_dir:
            sys.exit(f"[x] 自动探测失败: {err}\n    请用 --db-dir 手动指定 db_storage 路径。")
    if not os.path.isdir(db_dir):
        sys.exit(f"[x] 目录不存在: {db_dir}")
    log(f"  [√] db_storage: {db_dir}  (约 {db_count(db_dir)} 个库)")

    # 账号指纹（审计 E4）：缓存与微信数据目录绑定。多账号机器上若拿 A 账号的
    # 密钥/解密库去导 B 账号，会静默导错人 —— 指纹不符直接拦下。
    meta_path = os.path.join(args.cache, "wx_export.meta.json")
    if (os.path.isfile(keys) or os.path.isdir(dec)) and os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as mf:
                prev = json.load(mf).get("db_dir")
        except Exception:
            prev = None
        if prev and os.path.normcase(os.path.abspath(prev)) != os.path.normcase(os.path.abspath(db_dir)):
            sys.exit("[x] 缓存属于另一个微信数据目录，跨账号复用会导错数据:\n"
                     f"    缓存属于: {prev}\n    当前目标: {db_dir}\n"
                     "    处置: wx_export.py --purge --yes 清缓存重跑，或用 --cache 指定独立目录")
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump({"db_dir": os.path.abspath(db_dir)}, mf)

    # ---- 媒体导出（只需图片密钥 + 账号目录，不需要数据库密钥/解密库）
    if args.media:
        step(1, "提取图片密钥（读微信进程内存，需微信运行中；自动派生，无需打开图片）")
        media_keys = os.path.join(args.cache, "media_keys.json")
        account_dir = os.path.dirname(os.path.abspath(db_dir))
        if os.path.isfile(media_keys):
            log(f"  [→] 复用图片密钥缓存: {media_keys}")
        else:
            run([EXTRACT_IMG, "--account-dir", account_dir, "--out", media_keys])
            if not os.path.isfile(media_keys):
                sys.exit("[x] 图片密钥提取失败（微信是否在运行/已登录？）")
        step(2, "导出媒体")
        cmd = [EXPORT_MEDIA, "--account-dir", account_dir,
               "--keys", media_keys, "--out", args.outdir]
        if args.media != "all":
            cmd += ["--session", args.media]
        if args.media_video:
            cmd += ["--video"]
        if os.path.isdir(dec):
            cmd += ["--dec", dec]  # 会话名映射（有解密库时更友好）
        for fl in ("--since", "--until", "--last"):
            if getattr(args, fl[2:]):
                cmd += [fl, getattr(args, fl[2:])]
        run(cmd)
        return

    # ---- v2.3+ 新模块：只读已解密缓存（需之前跑过一次完整流程，decrypted 已就绪），不再提密钥 ----
    v23 = [a for a, on in (("--sns", args.sns), ("--favorite", args.favorite),
                           ("--biz", args.biz), ("--transfer", args.transfer),
                           ("--search", args.search), ("--incremental", args.incremental),
                           ("--digest", args.digest),
                           ("--all-sessions", args.all_sessions),
                           ("--watch", args.watch), ("--watch-all", args.watch_all)) if on]
    if v23:
        module_version = "v2.6" if args.watch or args.watch_all else "v2.3"
        step(1, f"{module_version} 模块：{v23[0]}（只读已解密缓存）")
        dec = os.path.join(args.cache, "decrypted")
        if not os.path.isdir(dec):
            sys.exit(f"[x] 未找到已解密缓存: {dec}\n"
                     "    v2.3+ 新模块只读解密库，请先用 --group/--user 跑一次完整流程（提密钥+解密）再用。")
        if not args.outdir:
            sys.exit("[x] v2.3 模块需要 --outdir 指定输出目录。")
        os.makedirs(args.outdir, exist_ok=True)
        def _time(cmd):
            for fl in ("--since", "--until", "--last"):
                if getattr(args, fl[2:]):
                    cmd += [fl, getattr(args, fl[2:])]
            return cmd
        if args.sns:
            run([EXPORT_SNS, "--dec", dec, "--out", os.path.join(args.outdir, "朋友圈.md")]
                + _time([]))
        elif args.favorite:
            run([EXPORT_FAVORITE, "--dec", dec, "--out", os.path.join(args.outdir, "收藏.md")]
                + _time([]))
        elif args.biz:
            run([EXPORT_BIZ, "--dec", dec, "--out", os.path.join(args.outdir, "公众号文章.md")]
                + _time([]))
        elif args.transfer:
            run([EXPORT_TRANSFER, "--dec", dec, "--kind", args.transfer,
                 "--out", os.path.join(args.outdir, "转账红包小程序.md")] + _time([]))
        elif args.search:
            run([SEARCH, "--dec", dec, "--keyword", args.search,
                 "--out", os.path.join(args.outdir, f"搜索_{args.search}.md")] + _time([]))
        elif args.incremental:
            run([EXPORT_INCREMENTAL, "--dec", dec, "--session", args.incremental,
                 "--out", os.path.join(args.outdir, re.sub(r'[\\/:*?"<>|]', "_", args.incremental) + "_聊天记录.md")]
                + _time([]))
        elif args.all_sessions:
            # v2.4：跨全部会话按时间批量导出。Markdown 汇总 + 同源 SQLite/JSON 底座（供上层 AI 总结事项）
            out_md = os.path.join(args.outdir, "全部会话汇总.md")
            out_db = os.path.join(args.outdir, "全部会话汇总_底座.db")
            out_js = os.path.join(args.outdir, "全部会话汇总_底座.json")
            run([EXPORT_ALL_SESSIONS, "--dec", dec, "--out", out_md,
                 "--sqlite", out_db, "--json", out_js] + _time([]))
        elif args.digest:
            # v2.5：全天跨会话梳理包（群+私聊，逐会话文件+小时分节+引用/转账/红包细分+_总览）。
            # 时间口径是 --date（不是 --since/--until/--last）；cap/merge-under 缺省值交给子脚本。
            cmd = [EXPORT_DAY_DIGEST, "--dec", dec, "--date", args.digest]
            if args.cap is not None:
                cmd += ["--cap", str(args.cap)]
            if args.merge_under is not None:
                cmd += ["--merge-under", str(args.merge_under)]
            run(cmd + ["--outdir", args.outdir])
        elif args.watch or args.watch_all:
            # 监听只读已解密缓存；输出与水位均落在显式 --outdir，避免把个人消息写进技能目录。
            out_path = os.path.join(
                args.outdir, "消息监听.jsonl" if args.watch_format == "jsonl" else "消息监听.txt"
            )
            state_path = os.path.join(args.outdir, "listener_watermark.json")
            cmd = [WATCH_MESSAGES, "--dec", dec, "--state", state_path,
                   "--out", out_path, "--format", args.watch_format,
                   "--interval", str(args.watch_interval)]
            if args.watch_all:
                cmd.append("--all")
            else:
                cmd += ["--session", args.watch]
            if args.watch_once:
                cmd.append("--once")
            if args.watch_since:
                cmd += ["--since", args.watch_since]
            run(cmd)
        log(f"\n[√] 完成，输出目录: {args.outdir}")
        return

    # ---- Step 1 密钥（有缓存就跳过）
    step(1, "提取数据库密钥（读微信进程内存，需微信运行中）")
    if os.path.isfile(keys):
        log(f"  [→] 复用缓存密钥（跳过扫描）: {keys}")
    else:
        os.makedirs(dump, exist_ok=True)
        log("  [i] 首次运行需扫描微信内存，约 30s ~ 8min ...")
        run_extract_keys(db_dir, dump, keys)
        if not os.path.isfile(keys):
            sys.exit("[x] 密钥提取未产出 all_keys.json（微信是否在运行/已登录？）\n"
                     "    若你的微信不是 4.1.13.x 系列（本技能实测 4.1.13.63），密钥格式可能已变化：\n"
                     "    请读 SKILL.md「微信机制·不变量」判断卡点层级，再按失效排查顺序定位。")
    log("  [√] 密钥就绪")

    # ---- Step 2 解密（缓存覆盖面完整才复用）
    step(2, "解密数据库")
    src_files = db_file_set(db_dir)
    n_src = len(src_files)
    def do_decrypt():
        run([DECRYPT, "decrypt", "--db-dir", db_dir, "--keys", keys, "--output", dec])

    missing = (src_files - db_file_set(dec)) if os.path.isdir(dec) else src_files
    if not missing:
        log(f"  [→] 复用缓存解密库（覆盖面完整 {n_src}/{n_src}）: {dec}")
    else:
        if len(missing) < n_src:
            log(f"  [!] 缓存解密库缺 {len(missing)} 个库（如 {sorted(missing)[0]}）-> 重新解密补齐"
                "（旧数量比例法对「微信新增分库」有盲区，审计 E3）")
        log(f"  [i] 解密中（约 3~6min，{n_src} 个库）...")
        do_decrypt()
        if len(db_file_set(dec)) < max(1, n_src // 2):
            # 密钥随微信重启/更新失效(踩坑#9)；缓存的旧密钥会解密失败 -> 重提后重试一次
            log("  [!] 解密结果偏少，疑似缓存密钥已失效 -> 重新提取密钥并重试")
            if os.path.isfile(keys):
                os.remove(keys)
            shutil.rmtree(dec, ignore_errors=True)
            os.makedirs(dump, exist_ok=True)
            run_extract_keys(db_dir, dump, keys)
            do_decrypt()
        # 二次校验（审计 E3）：自愈重试后仍缺过半就明确报错退出，绝不带着缺口报"就绪"
        if len(db_file_set(dec)) < max(1, n_src // 2):
            sys.exit(f"[x] 解密后仅得 {len(db_file_set(dec))}/{n_src} 个库，密钥可能已随微信版本更新失效。\n"
                     "    请读 SKILL.md「微信机制·不变量」判断卡点层级，再按失效排查顺序定位。")
        resid = src_files - db_file_set(dec)
        if resid:
            # 结构性不一致（解密工具改名/展平目录）属正常情形，只提示不拦截
            log(f"  [i] 解密库有 {len(resid)} 个文件名与源不一致（不影响使用，按数量口径放行）")
    log(f"  [√] 解密库就绪: {dec}  ({len(db_file_set(dec))}/{n_src})")

    # ---- 列群 / 列联系人
    if args.list_groups:
        step(3, "群列表")
        rows = list_groups(dec)
        log(f"共 {len(rows)} 个群：\n")
        for r in rows:
            nm = r["nick_name"] or "(无昵称)"
            log(f"  - {nm}")
        return
    if args.list_contacts:
        step(3, "联系人列表")
        rows = list_contacts(dec)
        log(f"共 {len(rows)} 个联系人：\n")
        for r in rows:
            nm = r["remark"] or r["nick_name"] or r["username"]
            log(f"  - {nm}")
        return

    # ---- Step 3 解析对象（群或私聊，各自消歧）
    if args.group:
        step(3, "定位群聊")
        g = resolve_group(dec, args.group)
        uname, gname = g["username"], (g["nick_name"] or g["username"])
        log(f"  [√] 目标群: {gname}   ({uname})")
    else:
        step(3, "定位联系人（私聊）")
        c = resolve_contact(dec, args.user)
        uname = c["username"]
        gname = c["remark"] or c["nick_name"] or c["username"]
        log(f"  [√] 目标联系人: {gname}   ({uname})")

    # ---- Step 4 导出 Markdown
    step(4, "导出 Markdown")
    # 文件名安全化：替换 Windows 非法字符后，把残留的 " _ " 收干净
    safe = re.sub(r'[\\/:*?"<>|]', "_", gname)
    safe = re.sub(r"\s*_\s*", "_", safe)
    out = args.out or os.path.join(args.outdir, safe + "_聊天记录.md")
    # 用 --username 精确定位（上游已消歧），下游不再做模糊匹配 —— 杜绝二次误配
    cmd = [EXPORT, "--dec", dec, "--username", uname, "--out", out]
    for fl in ("--since", "--until", "--last"):
        if getattr(args, fl[2:]):
            cmd += [fl, getattr(args, fl[2:])]
    if args.sqlite:
        cmd += ["--sqlite", args.sqlite]
    try:
        import zstandard  # noqa: F401
        cmd.append("--with-zstd")
        log("  [i] zstandard 可用 -> 自动解压压缩消息")
    except ImportError:
        log("  [!] 未装 zstandard -> 压缩消息将显示为占位符。"
            f" 建议: {sys.executable} -m pip install zstandard")
    run(cmd)
    log(f"\n[√] 完成: {out}")


if __name__ == "__main__":
    main()
