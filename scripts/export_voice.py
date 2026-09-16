#!/usr/bin/env python3
"""微信 4.x 语音导出（VoiceInfo 表提取 + SILK->WAV 解码，全 Python 原生）

原理（chatlog fork 开源实现验证）:
    微信把语音数据存在 <db_storage>/message/media_*.db 的 VoiceInfo 表
    （voice_data BLOB，SILK v3 格式，带 0x02#!SILK_V3 头 + 2B 帧长前缀）。
    消息表(local_type=34)的 server_id = VoiceInfo.svr_id，一一对应。
    语音不落文件系统（VoiceTemp 为空），但数据库里全量在，实测命中率 99.6%。

解码: 用 pysilk（silk-python，cffi 绑定的 Python 库，pip install silk-python）
    零 exe、零微信 DLL、零第三方服务。

用法:
    # 导出指定会话全部语音为 WAV
    python export_voice.py --dec "<沙盒>\\decrypted" --session "张三" --out "D:\\语音导出"

    # 导出全部会话语音（量大，谨慎）
    python export_voice.py --dec "<沙盒>\\decrypted" --out "D:\\语音导出"

输出:
    <out>/语音/<会话名或hash>/<日期>_<时间>_<序号>_<发信人>.wav
    <out>/语音/<会话名或hash>/语音时间线.csv         每会话时间线索引（与聊天记录同一时间源）
    <out>/语音/voice_map.json                       全局映射（svr_id->WAV），供 Markdown 导出
                                                    --voice-map 把语音嵌回聊天记录时间线

依赖:
    pip install silk-python     （--voice 时懒加载，缺失时提示安装）
"""
import argparse
import csv
import hashlib
import json
import os
import sqlite3
import struct
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def log(msg=""):
    print(msg, flush=True)


def find_db(dec, pattern):
    """在解密库目录中查找匹配文件（media_*.db / contact.db / message_*.db）"""
    found = []
    prefix = pattern.split("*", 1)[0] if "*" in pattern else None
    for root, _d, files in os.walk(dec):
        for fn in files:
            if prefix is not None:
                if fn.startswith(prefix):
                    found.append(os.path.join(root, fn))
            elif fn == pattern:
                found.append(os.path.join(root, fn))
    return sorted(found)


def load_nicknames(dec):
    """contact.db -> username -> 备注/昵称 映射（发信人显示用）"""
    nick = {}
    for cdb in find_db(dec, "contact.db"):
        try:
            conn = sqlite3.connect(cdb)
            rows = conn.execute(
                "SELECT username, remark, nick_name FROM contact").fetchall()
            conn.close()
            for username, remark, nick_name in rows:
                nick[username] = remark or nick_name or username
        except Exception:
            pass
    return nick


def resolve_session_hash(dec, name):
    """会话名称 -> 32 位表 hash（contact.db 里 username 的 md5）"""
    if len(name) == 32:
        return name.lower()
    nick = {}
    for cdb in find_db(dec, "contact.db"):
        try:
            conn = sqlite3.connect(cdb)
            rows = conn.execute(
                "SELECT username, remark, nick_name FROM contact "
                "WHERE remark LIKE ? OR nick_name LIKE ?",
                (f"%{name}%", f"%{name}%")).fetchall()
            conn.close()
            for username, remark, nick_name in rows:
                nick[username] = remark or nick_name or username
        except Exception:
            pass
    if len(nick) != 1:
        sys.exit(f"[x] 名称「{name}」匹配到 {len(nick)} 个会话，请用完整名称或 32 位 hash")
    return hashlib.md5(list(nick)[0].encode()).hexdigest()


def collect_voice_msgs(dec, session_hash):
    """遍历 message_*.db，收集目标会话语音消息 (server_id, create_time, username)"""
    msgs = []
    for db in find_db(dec, "message*.db"):
        try:
            conn = sqlite3.connect(db)
            tabs = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            tbl = f"Msg_{session_hash}"
            if tbl not in tabs:
                conn.close()
                continue
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info([{tbl}])")]
            if "server_id" not in cols or "create_time" not in cols:
                conn.close()
                continue
            # 本库 Name2Id：rid -> username（rid 局部于每个库，踩坑#20 同源逻辑）
            n2i = {}
            try:
                for r in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                    n2i[r[0]] = r[1]
            except Exception:
                pass
            rows = conn.execute(
                f"SELECT server_id, create_time, real_sender_id FROM [{tbl}] "
                "WHERE local_type=34 AND server_id IS NOT NULL").fetchall()
            conn.close()
            for svr_id, ct, rid in rows:
                msgs.append((svr_id, ct, n2i.get(rid, "")))
        except Exception as e:
            log(f"  [!] {os.path.basename(db)} 读取失败: {e}")
    return msgs


def load_voice_index(dec):
    """media_*.db VoiceInfo -> {svr_id: voice_data}（懒索引，不整表载入）"""
    dbs = find_db(dec, "media*.db")
    if not dbs:
        sys.exit(f"[x] 解密库中找不到 media_*.db（语音在 VoiceInfo 表，需先解密）")
    return dbs


def get_voice_data(media_dbs, svr_id):
    for db in media_dbs:
        try:
            conn = sqlite3.connect(db)
            r = conn.execute(
                "SELECT voice_data FROM VoiceInfo WHERE svr_id=? AND voice_data IS NOT NULL",
                (svr_id,)).fetchone()
            conn.close()
            if r and r[0] and len(r[0]) > 0:
                return r[0]
        except Exception:
            pass
    return None


def _pysilk_decode_bytes(data):
    """pysilk 解码为 PCM 字节（优先 bytes 接口，回退临时文件接口）"""
    import pysilk
    if hasattr(pysilk, "decode_bytes"):
        try:
            return pysilk.decode_bytes(data, 24000)
        except Exception:
            pass
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".silk", delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    try:
        out_pcm = tmp + ".pcm"
        with open(tmp, "rb") as fin, open(out_pcm, "wb") as fout:
            pysilk.decode(fin, fout, 24000)
        with open(out_pcm, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        for p in (tmp, tmp + ".pcm"):
            try:
                os.unlink(p)
            except OSError:
                pass


def silk_to_wav(data, out_path):
    """SILK 字节 -> WAV 文件（pysilk 懒加载依赖）"""
    try:
        import pysilk  # noqa: F401
    except ImportError:
        sys.exit("[x] 需要 silk-python：请先执行  pip install silk-python")
    pcm = _pysilk_decode_bytes(data)
    if not pcm:
        return False
    fs = 24000
    n = len(pcm) // 2
    with open(out_path, "wb") as f:
        f.write(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + n * 2, b"WAVE",
                            b"fmt ", 16, 1, 1, fs, fs * 2, 2, 16, b"data", n * 2))
        f.write(pcm)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dec", required=True, help="解密库目录（含 message/media_*.db）")
    ap.add_argument("--session", help="只导出指定会话（32 位 hash 或联系人/群名）")
    ap.add_argument("--out", required=True, help="语音输出目录")
    ap.add_argument("--limit", type=int, default=0,
                    help="最多导出 N 条（抽样调试用，0=全部）")
    args = ap.parse_args()

    if not os.path.isdir(args.dec):
        sys.exit(f"[x] 解密库目录不存在: {args.dec}")
    if not find_db(args.dec, "media*.db"):
        sys.exit(f"[x] {args.dec} 下找不到 media_*.db（请先解密数据库）")

    t0 = time.time()
    nick = load_nicknames(args.dec)
    media_dbs = load_voice_index(args.dec)
    log(f"  [i] media 库: {[os.path.basename(d) for d in media_dbs]}")

    # 会话范围
    sessions = None
    if args.session:
        h = resolve_session_hash(args.dec, args.session)
        sessions = [h]
        log(f"  [√] 会话「{args.session}」-> hash {h}")

    # 全库会话枚举（无 --session 时）
    all_tables = []
    for db in find_db(args.dec, "message*.db"):
        try:
            conn = sqlite3.connect(db)
            tabs = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            conn.close()
            all_tables.extend(tabs)
        except Exception:
            pass
    target_tables = [t for t in set(all_tables)] if not sessions else \
        [f"Msg_{s}" for s in sessions if f"Msg_{s}" in all_tables]
    target_tables.sort()

    if not target_tables:
        sys.exit("[x] 未找到任何语音消息分表")
    log(f"  [i] 覆盖 {len(target_tables)} 个会话分表")

    out_voice = os.path.join(args.out, "语音")
    os.makedirs(out_voice, exist_ok=True)
    stats = {"msg": 0, "hit": 0, "miss": 0, "wav": 0, "fail": 0, "decode_fail": 0}
    voice_map = {}   # {会话hash: {svr_id: {wav/ts/time/who/username/session}}}，供 Markdown 时间线嵌入
    for tbl in target_tables:
        h = tbl[4:]  # 去掉 Msg_ 前缀
        sname = None
        # 反查会话名（contact.db 里 username md5 == h）
        for cdb in find_db(args.dec, "contact.db"):
            try:
                conn = sqlite3.connect(cdb)
                r = conn.execute(
                    "SELECT username, remark, nick_name FROM contact").fetchall()
                conn.close()
                for username, remark, nick_name in r:
                    if hashlib.md5(username.encode()).hexdigest() == h:
                        sname = remark or nick_name or username
                        break
            except Exception:
                pass
            if sname:
                break
        sname = sname or h
        sdir = os.path.join(out_voice, sname)
        os.makedirs(sdir, exist_ok=True)

        # 收集该会话语音消息（复用 collect_voice_msgs：含本库 Name2Id -> username）
        msgs = collect_voice_msgs(args.dec, h)
        # 去重（跨库可能重复） + 时间排序
        seen = set()
        uniq = []
        for svr_id, ct, who_u in msgs:
            if svr_id in seen:
                continue
            seen.add(svr_id)
            uniq.append((svr_id, ct, who_u))
        uniq.sort(key=lambda x: x[1])
        if args.limit:
            uniq = uniq[: args.limit]

        for i, (svr_id, ct, who_u) in enumerate(uniq, 1):
            stats["msg"] += 1
            data = get_voice_data(media_dbs, svr_id)
            if not data:
                stats["miss"] += 1
                continue
            stats["hit"] += 1
            ts = datetime.fromtimestamp(ct).strftime("%Y-%m-%d_%H%M%S")
            who = nick.get(who_u, who_u or f"rid?") if who_u else f"rid?"
            out_path = os.path.join(sdir, f"{ts}_{i:03d}_{who}.wav")
            if silk_to_wav(data, out_path):
                stats["wav"] += 1
                # 时间线映射（聊天时间 = 消息表 create_time，与导出的 Markdown 同一时间源）
                voice_map.setdefault(h, {})[str(svr_id)] = {
                    "wav": os.path.relpath(out_path, args.out).replace("\\", "/"),
                    "ts": ct,
                    "time": datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M:%S"),
                    "who": who, "username": who_u or "", "session": sname}
            else:
                stats["decode_fail"] += 1

        # 每会话时间线 CSV（可直接按时间对齐聊天记录）
        if voice_map.get(h):
            csv_path = os.path.join(sdir, "语音时间线.csv")
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["序号", "聊天时间", "聊天记录显示时间(HH:MM)",
                            "发信人", "username", "svr_id", "WAV文件(相对导出根)"])
                for n, (svr_id, info) in enumerate(
                        sorted(voice_map[h].items(), key=lambda kv: kv[1]["ts"]), 1):
                    w.writerow([n, info["time"], info["time"][11:16],
                                info["who"], info["username"], svr_id, info["wav"]])
        log(f"  [√] {sname}: 语音 {len(uniq)} 条 -> WAV {stats['wav']}（累计）")

    # 全局映射：供 export_group_md.py --voice-map 把 WAV 嵌回聊天记录时间线
    if voice_map:
        map_path = os.path.join(out_voice, "voice_map.json")
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump(voice_map, f, ensure_ascii=False, indent=1)
        log(f"  [√] 时间线映射: {os.path.relpath(map_path, args.out)}"
            f"（{sum(len(v) for v in voice_map.values())} 条语音，"
            f"供 Markdown 导出 --voice-map 使用）")

    log(f"\n[√] 完成: {args.out}")
    log(f"  统计: 语音消息 {stats['msg']}，VoiceInfo 命中 {stats['hit']}"
        f"（未命中 {stats['miss']}），解码为 WAV {stats['wav']}，"
        f"解码失败 {stats['decode_fail']}，耗时 {time.time()-t0:.0f}s")
    if stats["decode_fail"]:
        log("  [!] 有解码失败项（pysilk 异常，可重试或检查数据）")


if __name__ == "__main__":
    main()
