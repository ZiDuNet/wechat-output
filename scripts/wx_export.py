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

设计要点:
    - 缓存复用：密钥/解密库缓存在 --cache 下，第二次导出别的群只需几秒（跳过耗时的提密钥+解密）。
    - 自动探测：从运行中的 Weixin.exe 定位安装目录 -> xwechat/config/*.ini -> 数据根目录。
      绿色版配置在安装目录旁，不在 AppData（技能文档旧说法有误）。
    - 安全消歧：群名匹配到多个候选时不瞎猜，列出候选并退出。
    - zstd：检测到 zstandard 就自动开启解压（实测约 80% 消息是压缩的，属刚需非可选）。
"""
import argparse
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRACT = os.path.join(HERE, "extract_keys_413.py")
DECRYPT = os.path.join(HERE, "wcdb_key_tool_windows.py")
EXPORT = os.path.join(HERE, "export_group_md.py")

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
    """运行中 Weixin.exe 的安装目录候选"""
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


def read_text(path):
    b = open(path, "rb").read()
    for enc in ("utf-8", "utf-16-le", "gbk"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", errors="replace")


def detect_db_dir():
    """自动探测 db_storage：ini -> 数据根目录 -> xwechat_files/<wxid>/db_storage"""
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


# ---------------------------------------------------------------- 主流程


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="群名（支持片段，唯一匹配即可）")
    ap.add_argument("--out", help="输出 Markdown 路径（默认 <outdir>/<群名>_聊天记录.md）")
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR,
                    help="输出目录（不设 --out 时导出文件放这里；必填，默认无）")
    ap.add_argument("--cache", default=DEFAULT_CACHE,
                    help="缓存目录（密钥+解密库，敏感；默认 ~/.wxcache）")
    ap.add_argument("--db-dir", help="手动指定 db_storage（跳过自动探测）")
    ap.add_argument("--list-groups", action="store_true", help="只列出所有群名后退出")
    ap.add_argument("--purge", action="store_true", help="删除缓存（密钥+解密库，敏感）")
    ap.add_argument("--sqlite", help="结构化输出 SQLite 路径（统计底座，可选）")
    args = ap.parse_args()

    if args.purge:
        if os.path.isdir(args.cache):
            shutil.rmtree(args.cache)
            log(f"[√] 已删除缓存: {args.cache}")
        else:
            log(f"[i] 缓存不存在: {args.cache}")
        return

    # 启动预检（人人可用）：缓存/输出目录所在盘符不存在时（os.makedirs 抛 WinError 3），
    # 把"中途崩"变成"起步时给清晰指引"。
    for label, p in (("--cache", args.cache),
                     ("--outdir", args.outdir if not args.out else None)):
        if not p:
            continue
        drive = os.path.splitdrive(os.path.abspath(p))[0]
        if drive and not os.path.isdir(drive + os.sep):
            sys.exit(f"[x] {label} 所在盘符不存在: {p}\n"
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

    # ---- Step 1 密钥（有缓存就跳过）
    step(1, "提取数据库密钥（读微信进程内存，需微信运行中）")
    if os.path.isfile(keys):
        log(f"  [→] 复用缓存密钥（跳过扫描）: {keys}")
    else:
        os.makedirs(dump, exist_ok=True)
        log("  [i] 首次运行需扫描微信内存，约 30s ~ 8min ...")
        run([EXTRACT, "--db-dir", db_dir, "--dump-dir", dump, "--out", keys])
        if not os.path.isfile(keys):
            sys.exit("[x] 密钥提取未产出 all_keys.json（微信是否在运行/已登录？）\n"
                     "    若你的微信不是 4.1.13.x 系列（本技能实测 4.1.13.63），密钥格式可能已变化：\n"
                     "    请读 SKILL.md「微信机制·不变量」判断卡点层级，再按失效排查顺序定位。")
    log("  [√] 密钥就绪")

    # ---- Step 2 解密（有缓存就跳过）
    step(2, "解密数据库")
    n_src = db_count(db_dir)
    n_dec = db_count(dec) if os.path.isdir(dec) else 0
    def do_decrypt():
        run([DECRYPT, "decrypt", "--db-dir", db_dir, "--keys", keys, "--output", dec])

    if n_dec >= max(1, int(n_src * 0.8)):
        log(f"  [→] 复用缓存解密库（跳过解密）: {dec}  ({n_dec}/{n_src})")
    else:
        log(f"  [i] 解密中（约 3~6min，{n_src} 个库）...")
        do_decrypt()
        if db_count(dec) < max(1, int(n_src * 0.5)):
            # 密钥随微信重启/更新失效(踩坑#9)；缓存的旧密钥会解密失败 -> 重提后重试一次
            log("  [!] 解密结果偏少，疑似缓存密钥已失效 -> 重新提取密钥并重试")
            if os.path.isfile(keys):
                os.remove(keys)
            shutil.rmtree(dec, ignore_errors=True)
            os.makedirs(dump, exist_ok=True)
            run([EXTRACT, "--db-dir", db_dir, "--dump-dir", dump, "--out", keys])
            do_decrypt()
    log(f"  [√] 解密库就绪: {dec}  ({db_count(dec)}/{n_src})")

    # ---- 列群
    if args.list_groups:
        step(3, "群列表")
        rows = list_groups(dec)
        log(f"共 {len(rows)} 个群：\n")
        for r in rows:
            nm = r["nick_name"] or "(无昵称)"
            log(f"  - {nm}")
        return

    if not args.group:
        sys.exit("[x] 需要 --group 群名（或用 --list-groups 查看）")

    if not args.out and not args.outdir:
        sys.exit("[x] 未指定导出位置：请用 --outdir 指定输出目录，或用 --out 指定文件路径。\n"
                 "    例: wx_export.py --group \"群名\" --outdir \"D:\\导出\"")

    # ---- Step 3 解析群（消歧）
    step(3, "定位群聊")
    g = resolve_group(dec, args.group)
    gname = g["nick_name"] or g["username"]
    log(f"  [√] 目标群: {gname}   ({g['username']})")

    # ---- Step 4 导出
    step(4, "导出 Markdown")
    # 文件名安全化：替换 Windows 非法字符后，把残留的 " _ " 收干净
    # （否则 "示例群A | 分群名" 会变成 "示例群A _ 分群名"）
    safe = re.sub(r'[\\/:*?"<>|]', "_", gname)
    safe = re.sub(r"\s*_\s*", "_", safe)
    out = args.out or os.path.join(args.outdir, safe + "_聊天记录.md")
    cmd = [EXPORT, "--dec", dec, "--group", gname, "--out", out]
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
