---
name: wechat-group-export
description: 从微信 Windows 4.x（实测 4.1.13.63，含新版 XOR 混淆密钥破解）本地加密数据库提取密钥、解密，导出指定群聊/私聊为 Markdown，并解密导出图片/视频，以及从 VoiceInfo 表提取语音解码为 WAV。数据库密钥与图片密钥均从进程内存自动提取（图片密钥由登录态 code 派生，全自动，无需打开图片）。零第三方依赖；解压富文本消息需 zstandard（1.8MB wheel）。触发词：导出微信群聊、微信聊天记录、微信解密、群聊备份、私聊导出、微信图片导出、微信媒体导出、微信数据库。
---

# 微信聊天记录导出全流程（群聊 / 私聊 / 媒体，Windows / 微信 4.1.13 实测）

> ⚠️ **平台支持状态（务必先读）**：本技能**仅在 Windows 上经过真机实测**。
> macOS / Linux 支持为 **v2.4 代码级实现，尚未在任何 macOS / Linux 真机测试**——相关脚本
> （`extract_keys_macos.py` / `extract_keys_linux.py` / `aes_backend.py` 的 darwin/linux 后端 /
> `media_common.py` 的 darwin/linux 分支）只做了语法与逻辑走查，**未经真机验证**。
> 在 macOS / Linux 上使用前，请先在一台真机验证密钥提取与解密全流程；因未真机测试导致的问题不在已实测范围内。

> 实测环境（2026-09-08）：Windows + 绿色版微信 4.1.13.63，数据目录由脚本自动探测（绿色/便携版在安装目录旁、安装版在 %APPDATA%），非管理员权限，零安装完成提取+解密+导出 23/23 库、3218 条群消息。
> 微信版本演进可能使本方法失效；失效时**先读下方「微信机制·不变量」判断卡在哪一层**（不变量版本无关，变的只是细节），再按「踩坑实录」末尾的失效排查顺序查对应细节。

## 红线（先读）

1. **只读**：全程不修改微信任何文件/进程，只读它的进程内存和本地库。不做批量采集、不碰别人账号。
2. **密钥敏感**：`all_keys.json`（数据库密钥）、`media_keys.json`（图片密钥）、解密库是敏感数据，只放沙盒/用户显式目录，绝不进 git、不上传。
3. **首次运行前必须获得用户明确同意**（涉及读取微信进程内存，用户可能担心账号风控）。
4. 解密产物放沙盒（`<session>\.sandbox-<task>\`），只有最终导出的 Markdown 放用户指定目录。

## 微信机制·不变量（以不变应万变，版本无关的解题思路）

> 本技能在 4.1.13.63 上经过 21 条踩坑调通。这些踩坑大多是**细节**（路径、字段名、偏移量），版本更新后可能变；但踩坑背后暴露的**机制**是不变的——微信是一套成熟的 C/S 架构产品，数据怎么存、身份怎么标、消息怎么加密，这些骨架不会因为一次版本升级推翻。适配新版本时，先判断卡在哪一层，再回踩坑表查细节，**不动摇不变量**。

### 第一层·加密：密钥只在内存里，且每次登录都换

- **不变量**：数据库密钥（key）**不存在任何本地文件里**，只在登录后的微信进程内存中；每次登录重新生成，重启微信就换。所以解题思路永远是"从进程内存里捞 key"，而不是"从文件里翻 key"。
- **不变量**：密钥的验证不靠版本号、不靠猜——用 `salt`（库头 16 字节）+ key 做 HMAC-SHA1 与库头 64~72 字节处的校验值比对，**数学上对就是对**。这个验证逻辑版本无关，变的只是 key 在内存里的形态（明文位置/偏移/是否加混淆，如 4.1.x 的 XOR 混淆）。
- **推论**：微信没运行/没登录 → 内存里没 key → 一切免谈。失效时第一步永远是确认微信活着、目标账号已登录。

### 第二层·数据架构：分片是常态，身份映射每库各自为政

- **不变量**：数据按功能分库放在 `db_storage` 下——`message/`（消息）、`contact/`（联系人）、`session/`（会话）、`publicmsg/`（公众号）、`media/`（图片视频文件）。分库分工不会变。
- **不变量**：消息库**分片是常态**（`message_0.db` ~ `message_N.db`），一个群的聊天记录散落在多个库里，**必须全收集、逐库解密、合并排序**，只解一个库 = 只拿到残缺数据。
- **不变量**：**每个 message_N.db 自带一张 `Name2Id(user_name, is_session)` 表**（rowid → username），这是消息里发信人序号的**权威映射**。序号空间**按库独立**——同一个人在 message_0.db 可能是 rid 4、在 message_1.db 是 rid 9，绝不能拿一个库的序号去另一个库（或全局表）里查人。
- **不变量（雷区）**：contact.db 的全局 `name2id` 表 rowid 1~9 是系统账号（floatbottle/mphelper/qqmail 等小号），**绝不能**拿全局表的 rowid 去解析消息里的发信人序号——否则大量消息被张冠李戴到系统小号头上。全局 id2name 只允许用于成员核验（chatroom_member.member_id = 全局 rowid）。

### 第三层·消息本体：内容即身份，消息自己说清自己是谁发的

- **不变量**：发信人解析的**两级定案**——① 他人消息正文必带前缀 `"<username>:\n"`，前缀就是发信人真身；② **微信不给自己加前缀**，无前缀的可归因消息 = 本人发的，用所在库 Name2Id 反查 rid 确认。
- **不变量**：解析阶梯固定为 **前缀 > 本库 Name2Id > refermsg 反查**（引用消息的 `<refermsg><svrid>+<chatusr>` 可仲裁无前缀消息的真身）。拿不准身份时用 refermsg 交叉互证，不要猜。
- **不变量**：消息正文可能是 zstd 压缩的（`WCDB_CT_message_content=4` 属性，或内容以 magic `\x28\xb5\x2f\xfd` 开头），必须解压；这个机制由 WCDB 框架决定，版本间稳定。
- **不变量**：`local_type` 的语义分层不变——0=1 文本、3 图片、34 语音、43 视频、47 表情、48 位置、10002 撤回、10000 系统通知；五位大数（如 266287972401 拍一拍）是"引用/动作类复合类型"，按系统事件显示。**系统消息（10000/10002/拍一拍）没有发信人属性，不算进发言者名单**。
- **不变量（槽位）**：db 内存在固定的"群身份"rid（如 121）在 Name2Id 里留空，承载撤回/拍一拍/入群通知及"以群身份发出的内容"，显示"未知ID"是**数据属性**，不是解析 bug。

### 第四层·方法论：怎么调试都不会错的原则（跟微信版本无关）

1. **先 dump 再动手**：任何字段拿不准，先把表结构和样本数据 dump 出来看，禁止靠猜写逻辑。
2. **身份必须双向互证**：单一路径（只靠前缀/只靠 rid）都出过张冠李戴；两条独立路径得出同一人，才算数。
3. **去重键 = 消息身份**：UNIQUE 约束比消息身份粗（如"同秒同人同文"）就会静默吞消息；拿不准就整群重写（DELETE+INSERT），幂等且零误删。
4. **完整性自检内置在流程里**：解密几个库、导出几条、库账是否相等、发送者是否都在成员名单内——数字对不上就是有问题，不许跳过。
5. **容错不一票否决**：个别消息解析失败标记占位符继续走，绝不因单条失败放弃整群；但**失败计数要如实报告**。

### 新版本适配判断顺序（失效时照此排查）

| 症状 | 卡在哪层 | 先查什么 |
|---|---|---|
| 解密失败 / 校验不过 | 第一层 | 微信是否登录；key 提取偏移/混淆方式变了 → 踩坑 #1~#5 |
| 找不到群 / 数据残缺 | 第二层 | db_storage 路径变了；分片收集不全 → 踩坑 #6~#10 |
| 内容乱码 / 发信人错 | 第三层 | 压缩机制、前缀规则、Name2Id 结构 → 踩坑 #15~#21 |
| 数字对不上 / 重复 / 丢失 | 第四层 | 去重键、完整性自检逻辑 → 踩坑 #21 + 自查表 |

**判断原则：先定位层级，再查对应细节。细节允许变，四层不变量不动。**

## 工具清单

| 需要 | 说明 |
|---|---|
| Python 3.10+ | 任意本机 Python（脚本零第三方依赖） |
| 本技能 `scripts/` 全部脚本 | `extract_keys_413.py`（数据库密钥提取+破解）、`export_group_md.py`（群/私聊导出）、`extract_image_key.py`（图片密钥自动提取）、`export_media.py`（媒体导出）、`media_common.py`（共享库）；v2.1+ 语音 `export_voice.py`、v2.2+ 媒体索引 `export_media_index.py`、文件 `export_files.py`；**v2.3+ 新增**：朋友圈 `export_sns.py`、收藏 `export_favorite.py`、服务号 `export_biz.py`、转账红包小程序 `export_transfer.py`、聊天搜索 `search_messages.py`、增量导出 `export_incremental.py`；**v2.4+ 新增**：统计分析台 `chat_stats.py`、群素材包 `digest_source.py`、跨全部会话批量导出 `export_all_sessions.py`；**v2.5+ 新增**：全天跨会话梳理包 `export_day_digest.py`（逐会话文件+小时分节+引用/转账/红包细分）；**v2.6+ 新增**：只读实时监听 `watch_messages.py`（跨分片增量、水位持久化、JSONL/text 输出） |
| `wcdb_key_tool_windows.py` | 密钥校验/解密函数来源（GitHub: TANGandXUE/wcdb-key-tool，MIT）。若本技能 scripts 未带，从该仓库取 |
| zstandard（**实际必需**） | 解压压缩消息。`WCDB_CT_message_content=4` 或以 `\x28\xb5\x2f\xfd` 开头的消息都靠它。实测压缩占比很高（某读书群 161/201=**80%**、某时间管理群 496/1833=27%），**不装的话这些内容全变成 `[压缩未解]` 占位符**。1.8MB wheel，装隔离 venv |
| sqlite3 / hashlib / ctypes | 全部标准库 |

**不需要**：`wechat-cli` pip 包（PyPI 上不存在，别装）、`pycryptodome`（Windows 走系统 bcrypt.dll）、管理员权限（非管理员可读同用户微信进程内存，实测）。

### 可选依赖（增强功能）

| 需要 | 说明 | 用途 |
|---|---|---|
| pysqlcipher3 | `pip install pysqlcipher3` | **首选**直连加密库，免解密到磁盘 |
| sqlcipher | `apt install sqlcipher` / `brew install sqlcipher` | CLI 回退方案，pysqlcipher3 不可用时用 |

## v3.0 架构增强：直连加密库 + 高级查询

> v3.0 引入 `wcdb_core.py` 统一数据库访问层，**直连加密库**替代先解密到磁盘的旧方案。
> 旧方案保留为降级备选。

### 数据库访问架构

```
旧方案（降级备选）：
  extract_keys → decrypt_all(decrypted/) → sqlite3.connect(明文.db)

新方案（首选）：
  extract_keys → pysqlcipher3.connect(加密.db, key=enc_key)
                  ↓
              wcdb_core.WcdbSession 统一入口
                  ↓
    ┌─────────────┼─────────────┐
    │             │             │
  查询模块    搜索模块    统计模块
```

### 新增模块清单

| 模块 | 功能 | CLI |
|---|---|---|
| `wcdb_core.py` | 统一数据库访问层（pysqlcipher3 / sqlcipher CLI / 明文降级三后端） | `python wcdb_core.py info/query/scan` |
| `search_fts5.py` | FTS5 全文搜索（比 LIKE 快 100x+） | `python search_fts5.py --query "关键词"` |
| `cursor_fetch.py` | 游标分批拉取（大群不 OOM） | `python cursor_fetch.py --session "群名" --batch 500` |
| `contacts.py` | 联系人/群组查询（昵称/备注/成员/头像） | `python contacts.py contact/search/members/groups/stats` |
| `hardlink.py` | 硬链接解析（图片/视频 md5 → 实际路径） | `python hardlink.py image/video/list-dbs` |
| `db_health.py` | 数据库健康检查（完整性/分片/大小） | `python db_health.py --quick` |
| `exec_query.py` | 通用 SQL 执行器（任意 SQL 查加密库） | `python exec_query.py query "SELECT ..."` |
| `anti_revoke.py` | 消息反撤回（⚠️ 可选，修改数据库） | `python anti_revoke.py install/check/restore` |
| `stats.py` | 统计分析（总览/会话/聚合） | `python stats.py overview/session/aggregate` |

### 用法示例

```python
# 直连加密库查询
from wcdb_core import WcdbSession
with WcdbSession(db_dir="db_storage", enc_key="64hex...") as db:
    rows = db.query("SELECT * FROM contact LIMIT 10")

# FTS5 全文搜索
from search_fts5 import FtsSearcher
with FtsSearcher("db_dir", enc_key="64hex...") as s:
    results = s.search("关键词", session_id="xxx@chatroom")

# 游标分批拉取
from cursor_fetch import MessageCursor
with MessageCursor("db_dir", enc_key="64hex...", session_id="xxx@chatroom") as c:
    for batch in c.batches(batch_size=500):
        for msg in batch:
            process(msg)

# 联系人查询
from contacts import ContactManager
with ContactManager("db_dir", enc_key="64hex...") as cm:
    contact = cm.get_contact("wxid_xxx")
    members = cm.get_group_members("xxx@chatroom")

# 数据库健康检查
from db_health import DbHealthChecker
with DbHealthChecker("db_dir", enc_key="64hex...") as checker:
    report = checker.full_check()
```

### 降级策略

当 pysqlcipher3 和 sqlcipher 均不可用时，自动降级为旧方案：
1. `extract_keys_413.py` 提取密钥（不变）
2. `wcdb_key_tool_windows.py decrypt` 解密到磁盘（不变）
3. 各导出脚本读明文 .db（不变）

## 一键用法（推荐，日常只记这条）

`wx_export.py` 把 Step 0~4 串成一条命令：**自动探测数据目录 + 缓存复用 + 安全消歧 + 失效自愈**。

```bash
PY="python"   # 任意装有 zstandard 的 Python 3.10+ 环境
cd "<技能目录>/scripts"
OUT="D:/微信群导出"                       # ← 改成你自己的导出目录（必填）
DB="$OUT/wechat_stats.db"                 # ← 统计底座库（可选；digest 技能要用同路径）

"$PY" wx_export.py --group "群名" --outdir "$OUT"                  # 导出群聊（全自动）
"$PY" wx_export.py --user "联系人备注" --outdir "$OUT"            # 导出私聊
"$PY" wx_export.py --media all --outdir "$OUT/媒体"                # 导出全部图片（自动提密钥+解密）
"$PY" wx_export.py --media "联系人" --media-video --outdir "$OUT/媒体"  # 导出某人图片+视频
"$PY" wx_export.py --list-groups         # 列出全部群名（秒级，用来确认群名）
"$PY" wx_export.py --list-contacts       # 列出全部联系人（秒级）
"$PY" wx_export.py --all-sessions --last 昨天 --outdir "$OUT"   # 跨全部会话按时间批量导出汇总（v2.4）
"$PY" wx_export.py --digest yesterday --outdir "$OUT/昨天"     # 全天跨会话梳理包：总览+逐会话文件+小时分节（v2.5）
"$PY" wx_export.py --digest 2026-09-16 --outdir "$OUT" --merge-under 100   # 指定日期；小会话并入合集
"$PY" wx_export.py --watch "群名" --outdir "$OUT/监听"       # 只读实时监听，输出 JSONL + 持久化水位（v2.6）
"$PY" wx_export.py --watch-all --watch-once --outdir "$OUT/监听"  # 全部会话只轮询一轮
"$PY" wx_export.py --purge               # 清缓存（密钥+解密库，敏感）
```

- 解释器用**装了 zstandard 的 Python 3.10+ 环境**（上面 `$PY` 即该环境）。
- 缓存默认 `~/.wxcache`（`--cache` 可改）。**有缓存后导出下一个群约 0.5 秒**（实测首次 ~6min → 复用 0.46s）。
- **缓存复用判定 = 按库文件名集合比对，不是数量比例**（2026-09-09 改）：源 `db_storage` 里每个 `.db` 在解密缓存中都有对应文件才复用，缺一个就重新解密补齐。旧的数量比例法（解密数 ≥ 源数×0.8）有盲区——微信新增 `message_24.db` 后 23/24 仍满足阈值，新库会**静默漏解**导致导出少数据。解密后仍缺过半会直接报错退出，绝不带着缺口报"就绪"。
- **跨账号保护**：缓存与微信数据目录绑定（缓存内 `wx_export.meta.json` 记录 `db_dir`）。多账号机器上缓存若属于另一个数据目录，直接拒绝复用并提示 `--purge --yes`，避免拿 A 账号的解密库导 B 账号的数据。
- **末尾会打一行「消息口径对账」**：`本次共 N 条 = 有效 X 条 + 系统/撤回/拍一拍 Y 条`。统计底座 `groups.msg_count` 与群刊「有效消息」**同源采用 X**（不含系统消息）；看到导出日志与群刊数字不一致，先对这行。
- 群名命中多个候选时**不瞎猜**，列出候选并退出 1（测试："群" 命中 307 个 → 列出退出）。单跑 `export_group_md.py` 同样不瞎猜，且**只在 `@chatroom` 里找群**（关键词撞上好友昵称时绝不把私聊当群导出）。
- 缓存密钥失效（微信重启/更新会换密钥）→ 自动重提密钥并重解一次，无需人工干预。
- 手动指定数据目录：`--db-dir "...\db_storage"`。
- `--sqlite <路径>`：同一次解析额外产出结构化 SQLite（groups/members/messages 三张表，统计底座）。多群可写入**同一座库**累积；重复导出同一群不翻倍（整群重写）。三张表口径：`groups`（每群一行，`msg_count` = **有效消息数，不含系统消息**）/ `members`（`room_id+username` 唯一，`is_current` 每轮导出重新计算，退群者会置 0）/ `messages`（逐条消息，`is_system=1` 表示系统/撤回/拍一拍）。

> 只有需要逐步排错时，才走下面 Step 1~4 手动流程。

### 首次运行自检清单（新用户 / AI 拿到本技能后按序自查，全部可自动完成）

本技能按"程序与数据分离"发布：脚本**不写死任何机器路径**。首次运行前按下面顺序自查，缺什么补什么——不要直接跑主命令撞错：

1. **Python**：`python --version`（需 3.10+，Windows 版）。
2. **zstandard**：`python -c "import zstandard"`；失败则 `python -m pip install zstandard`（1.8MB）。
   没装不会报错，但压缩消息会显示为 `[压缩未解]` 占位符（实测约 80% 富文本消息被压缩，属刚需）。
3. **微信在跑且已登录**：`tasklist | findstr /i weixin`——密钥只存在于运行中的微信进程内存，没登录一切免谈。
4. **微信版本**：`powershell "(Get-Process Weixin | Select -First 1).VersionInfo.ProductVersion"`。
   本技能实测 **4.1.13.63**；4.1.13.x 同系列应可运行。版本不符时密钥提取会失败（此时脚本会给出诊断），
   按下方「新版本适配判断顺序」逐层排查，而不是怀疑脚本坏了。
5. **导出位置**：选一个自己的目录（如 `D:/微信群导出`），主命令带 `--outdir` 显式指定；
   缓存默认 `~/.wxcache`（用户主目录），也可 `--cache` 改。脚本拒绝在未指定输出位置时运行。
6. **冒烟验证**：`wx_export.py --list-groups` 能列出群名 = 整条提密钥+解密链路 OK（首次约 30s~8min，之后秒级）。

## 前置检查（Step 0）

```bash
# 1. 微信在跑且已登录（密钥从进程内存提取）
tasklist | grep -i weixin        # Weixin.exe=4.x 新版
# 2. 微信版本（决定路线）
powershell "(Get-Process Weixin | Select -First 1).Path"   # 安装位置
# 3. 数据目录：读 ini
#    ⚠️ 绿色/便携版的配置在【安装目录旁】，不在 AppData（旧文档写 AppData 是错的）
powershell "(Get-Process Weixin | Select -First 1 -ExpandProperty Path)"  # -> <微信安装目录>\Weixin.exe
cat "<微信安装目录>\xwechat\config\"*.ini        # 其中一行即数据根目录（如 X:\微信数据）
#    安装版兜底：%APPDATA%\Tencent\xwechat\config\*.ini
#    拿到根目录后：xwechat_files\<wxid>\db_storage（多账号取 .db 最多的那个）
# 4. 账号目录可能有多个，db_storage 对应目录大小从几十 MB 到几 GB 不等
```

## 全流程

### Step 1 提取密钥（本技能脚本，一条命令）

```bash
python extract_keys_413.py \
  --db-dir "<数据根目录>\xwechat_files\<wxid>\db_storage" \
  --dump-dir "<沙盒>\config_dump" \
  --out "<沙盒>\all_keys.json"
```

- 内部流程：扫微信进程内存找 `com.Tencent.WCDB.Config.Cipher` 对象 → dump blobs → 去重 → **逐位置破解 XOR 混淆**（原理见踩坑#3）→ 解出 `x'<64hex key><32hex salt>'` → salt 与库文件第一页真实 salt 硬比对 + HMAC 校验 → 输出 `{rel: {enc_key, salt}}`。
- 耗时：内存扫描 2-8 分钟 + 破解几秒。**超时给足 600s**。
- 期望输出 `23/23` 这类全量命中；个别非核心库（`weclaw.db`、`solitaire.db`）失败不影响聊天导出。
- 若 `Total blobs dumped: 0`：微信版本又变了，见踩坑#8。

### Step 2 解密全部库（用 wcdb_key_tool_windows.py）

```bash
python wcdb_key_tool_windows.py decrypt \
  --db-dir "<db_storage>" \
  --keys "<沙盒>\all_keys.json" \
  --output "<沙盒>\decrypted"
```

- 884MB 约 3-6 分钟，期望 `23 成功, 0 失败`。
- 产出标准 SQLite，直接可查。

### Step 3 定位群聊 + 导出（本技能脚本）

```bash
python export_group_md.py \
  --dec "<沙盒>\decrypted" \
  --group "示例群名" \
  --out "<导出目录>/<群名>_聊天记录.md"
# --with-zstd  加上此开关且已装 zstandard 时，解压富文本消息
```

- 自动完成：contact 表按昵称找群 → `Msg_<md5(username)>` 分表定位（遍历所有 message*.db）→ sender 映射（`contact.db` 的 `name2id` 表 **rowid → username**）→ 按天分组输出 Markdown。
- 关键表结构（4.1.13 实测）：
  - `contact\contact.db`：`contact`(username, nick_name, local_type, remark)、`name2id`(仅一列 username，**rowid 即消息里的 real_sender_id**)
  - `message\message_*.db`：`Msg_<md5>` 分表，列 `local_type`(1=文本/3=图/34=语音/43=视频/47=表情/49系列=富文本/10000=系统)、`real_sender_id`、`create_time`(unix秒)、`message_content`、`WCDB_CT_message_content`(4=zstd 压缩)
  - 群聊明文文本自带 `"wxid_xxx:\n内容"` 前缀，导出时去掉（sender 以映射为准）

### Step 4（可选）补全压缩消息

```bash
# 1.8MB wheel，装进隔离 venv，勿装系统 Python（切勿装到系统 Python）
# 本机实测命令（managed 隔离 venv，3.13.12）：
python -m pip install zstandard
# ⚠️ 别用 venv 里的 pip.exe 直接装——实测静默无输出、exit 0 但根本没装上；必须用 python -m pip。
# 重跑 Step 3 加 --with-zstd
```


## 媒体导出（图片 / 视频 / 语音，v2.0+ 新增）

> 实测（2026-09-17）：#[MOTHER] 私聊媒体 1758 张图片全部解密成功（失败 0），另复制视频 130 个，
> 全程约 10s。图片密钥**全自动提取**——扫微信进程内存里的登录态整数 code（常驻，实测数百处副本），
> 用 `md5(code+wxid)` 派生 AES 密钥，模板密文验证通过即采信，**无需用户打开图片**。

### 图片存储与格式

| 位置 | 说明 |
|---|---|
| `msg/attach/<会话hash>/<YYYY-MM>/Img/<md5>.dat` | 图片缓存。V2 加密格式：`070856320807` + aes_size + xor_size + pad → AES 区(ECB+PKCS7) + 明文区 + XOR 区 |
| `msg/video/` | 视频为**明文 mp4**，直接复制即可，无需解密 |
| 语音 | **存在数据库里**（`message/media_*.db` 的 `VoiceInfo` 表，`voice_data` BLOB），语音不落文件系统（`VoiceTemp` 目录恒空）——见下方语音导出 |

### 图片密钥派生（账号级固定，微信版本无关的数学规律）

- `aes_key = md5(f"{code}{wxid}")[:16]`（16 字符 ASCII，当 16 字节 AES key）
- `xor_key = code & 0xFF`
- **code 是登录态整数（uin 类），常驻微信进程内存**：直接扫 4 字节 LE 窗口（低字节 = XOR 预过滤），
  逐候选派生密钥并用 V2 模板密文（首个 AES 块）验证 → 命中即永久保存 `media_keys.json`
- **wxid 用短名**（去掉 `_设备后缀`，如 `wxid_xxx_8bb3` → `wxid_xxx`）——实测派生用的是短名，
  脚本会自动尝试两种形态
- 密钥不随微信重启变化（code 由账号决定），`media_keys.json` 可长期复用；换账号才需重提

### 格式与转码

- 直接解密可得到 jpg / png / webp / gif
- **WXGF**（微信自研容器，本地只有缩略图缓存）：解密后仍为 wxgf，需用微信自带
  `VoipEngine.dll` 的 `wxam_dec_wxam2pic_5` 转码为 jpg（自动从微信安装目录探测 DLL，
  实测 107 张转码全成功，82KB 缩略图 → 929KB 完整原图）

### 媒体导出用法（Step 3'，独立于数据库解密）

```bash
# wx_export 集成（推荐）：自动提图片密钥（缓存复用）+ 导出
python wx_export.py --media "联系人/群名" --media-video --outdir "D:/媒体导出"
python wx_export.py --media all --outdir "D:/媒体导出"          # 全部会话

# 单独跑（调试用）
python extract_image_key.py --account-dir "D:/微信数据/xwechat_files/<wxid>" --out "media_keys.json"
python export_media.py --account-dir "D:/微信数据/xwechat_files/<wxid>" \
    --keys "media_keys.json" --out "D:/媒体导出" --session "联系人" --video
```

- 媒体导出**不需要数据库密钥/解密库**（图片密钥独立于 DB 密钥）；但给 `--dec` 解密库时
  会把会话 hash 目录映射成可读名称（#[MOTHER] 而非 32 位 hash）
- 输出结构：`<out>/图片/<会话名或hash>/<月份>_<md5>[_t].jpg`；WXGF 转码的完整原图去掉 `_t` 后缀
- 首次约 10~30s（扫内存提 code + 解密），之后复用 `media_keys.json` 秒级完成

### 语音导出（v2.1 新增，全 Python 原生，零 exe / 零 DLL / 零第三方服务）

> **关键发现（chatlog fork 开源实现验证，纠正"语音不落盘"的旧结论）**：微信把语音数据
> **存在数据库里**——`<db_storage>/message/media_*.db` 的 `VoiceInfo` 表（`voice_data` BLOB，
> SILK v3 格式，`0x02#!SILK_V3` 头 + 2B 帧长前缀）。消息表 `local_type=34` 的
> `server_id` = `VoiceInfo.svr_id`，一一对应。实测 #[MOTHER] 6353 条语音消息
> 6329 条在 VoiceInfo 命中（**99.6%**），未命中 24 条为过期/撤回等罕见情况。
> 之前"全盘扫不到音频文件"是因为语音根本不落文件系统，扫错地方了。

- 解码用 **pysilk**（`pip install silk-python`，cffi 绑定的 Python 库）——不下载 exe、
  不调微信 DLL、不绑微信版本。`--voice` 时懒加载，缺失时提示安装。
- **语音消息正文列是 zstd 压缩的 `<voicemsg>` 元数据 XML**（时长/格式/CDN 引用，**不是语音内容**，
  语音本体在 VoiceInfo 表已单独解成 WAV）。`--with-zstd` 解开后 Markdown 显示时长：
  - 新版结构带 `length=毫秒` → `[语音 15s]`（精确）
  - 旧版结构只有 `voicelength=字节`（SILK 约 1KB/s）→ `[语音 ~4s]`（估算，标 ~）
  - 无论哪种结构，voicemsg XML **绝不泄漏进 Markdown**（审计：6353 条语音行 XML 残留=0）
- 依赖解密库（语音在解密后的 media_*.db，不像图片那样独立于 DB 密钥）。

```bash
# 导出指定会话语音为 WAV（32 位 hash 或联系人/群名）
python export_voice.py --dec "<解密库目录>" --session "#[MOTHER]" --out "D:/语音导出"

# 导出全部会话（量大，谨慎；--limit N 可抽样调试）
python export_voice.py --dec "<解密库目录>" --out "D:/语音导出" --limit 10
```

- 输出：`<out>/语音/<会话名或hash>/<日期>_<时间>_<序号>_<发信人>.wav`（24kHz 单声道 WAV）
- 发信人 = 消息所在库 `Name2Id`（rid→username，局部于库，踩坑#20 同源逻辑）→ contact 备注/昵称
- 转文字（whisper / 云 API）**留作可插拔后端**：`voice_data` 解码后是标准 WAV，
  接任何 ASR 都是喂文件即可，不阻塞
- **时间线可追溯（聊天时间 = 消息表 create_time，与导出的 Markdown 同一时间源）**：
  - 文件名自带秒级聊天时间：`<日期>_<时间>_<序号>_<发信人>.wav`
  - 每会话输出 `语音时间线.csv`（序号/聊天时间/显示时间 HH:MM/发信人/svr_id/WAV 相对路径），
    Excel 可直接打开按时间对齐聊天记录
  - 全局输出 `voice_map.json`（svr_id → WAV 相对路径 + 时间 + 发信人），
    给 Markdown 导出用 `--voice-map` 嵌入：
    ```bash
    # 先导语音（产出 voice_map.json），再导聊天记录（语音消息行自动嵌上 WAV 路径）
    python export_voice.py --dec "<解密库>" --session "#[MOTHER]" --out "D:/导出" --limit 100
    python export_group_md.py --dec "<解密库>" --username "wxid_xxx" --out "D:/导出/私聊.md"         --voice-map "D:/导出/语音/voice_map.json"
    ```
    效果：聊天记录里 `[语音] 🎤 语音/#[MOTHER]/2023-09-02_192148_001_#[MOTHER].wav`
    直接对应到当天该时刻的那条语音。

### 媒体索引（文件/视频/图片 → 本地路径，v2.2 新增）

场景：**"帮我拿一下和 XXX / XXX 群里面的文件/视频"** —— 只返回本地缓存路径，
不复制、不移动、不修改微信数据。主入口 `export_media_index.py`：

```bash
# 某会话的文件（XML title ↔ msg/file 明文文件，消息级精确关联）
python export_media_index.py --account-dir "<账号目录>" --dec "<解密库>" --session "晓东" --type file
# 某会话的图片缓存（会话级：attach/<hash>/<月>/Img/*.dat，V2 加密，需 --media 解密才可用）
python export_media_index.py --account-dir "<账号目录>" --dec "<解密库>" --session "#[MOTHER]" --type image
# 全部视频（msg/video 无会话维度，只能全量+缩略图识别）
python export_media_index.py --account-dir "<账号目录>" --dec "<解密库>" --type video
# 时间过滤：今天 / 昨天 / 近 7 天 / 近 3 个月（与 --last 通用）
python export_media_index.py --account-dir ... --dec ... --type all --last 7d
# 结果写 JSON（含路径/时间/会话/大小）
python export_media_index.py --account-dir ... --dec ... --type file --session "X" --out "./idx"
```

**三种媒体的存储与关联边界（实测，勿凭感觉改）**：

| 媒体 | 本地存储 | 关联方式 | 说明 |
|------|---------|---------|------|
| 文件 | `msg/file/<月>/<原文件名>` 明文 | XML title ↔ 文件名（消息级 ✅） | 重名带 `(1)` 前缀容错 + 大小校验 |
| 图片 | `attach/<会话hash>/<月>/Img/<md5>.dat` | 会话级 ✅（目录名=会话 hash） | ⚠️ XML 的 md5 与 dat 文件名**交集=0**（微信用另一套命名），**无法消息级**；dat 为 V2 加密，需 `wx_export.py --media` 解密成可用图片 |
| 视频 | `msg/video/<月>/<md5>.mp4` 明文 | **无法按会话** | XML md5 与 mp4 文件名交集=0，mp4 文件名也不出现在消息 XML 任何字段；只能全量列出 + 缩略图人工识别 |

**微信机制（用户亲口确认 + 全量测试佐证）**：只有**点开/下载过**的媒体才会落盘。
未命中的媒体 = 未在微信客户端点开过，本地拿不到（工具无法凭空下载）。
视频缩略图 `_thumb.jpg` 与 mp4 同名同目录，可作识别线索。

### 按时间范围导出（今天 / 昨天 / 近 N 天·周·月·年 / 全部）

所有导出脚本统一支持 `--last` / `--since` / `--until`（media_common.parse_time_range，
大小写与中英均可）：

| 用户说法 | 命令参数 | 效果 |
|---------|---------|------|
| 今天的 | `--last today` 或 `--last 今天` | 今天 00:00 ~ 23:59 |
| 昨天的 | `--last yesterday` / `--last 昨天` | 昨天 00:00 ~ 昨天 23:59 |
| 近 7 天 | `--last 7d` / `--last 近7天` | 含今天往前 7 天 |
| 近 2 周 | `--last 2w` / `--last 近2周` | 含今天往前 2 周 |
| 近 3 个月 | `--last 3m` / `--last 近3月` | 含今天往前 3 月 |
| 近 1 年 | `--last 1y` / `--last 近1年` | 含今天往前 1 年 |
| 全部 | `--last all` / `--last 全部` / 不传 | 不过滤 |
| 自定义区间 | `--since 2026-09-01 --until 2026-09-10` | 含边界；until 省略时刻按当日 23:59:59 |

各脚本接入情况：

```bash
# 聊天记录 Markdown（按消息 create_time 精确过滤）
python export_group_md.py --dec "<解密库>" --username "wxid_xxx" --out "近7天.md" --last 7d
# 语音导出（按消息 create_time）
python export_voice.py --dec "<解密库>" --session "晓东" --out "D:/语音" --last yesterday
# 媒体索引（文件按消息时间；图片按缓存月份目录近似；视频按文件 mtime 近似）
python export_media_index.py --account-dir "..." --dec "..." --session "XX群" --type file --last 30d
# 图片解密导出（按缓存月份目录近似）
python export_media.py --account-dir "..." --keys "..." --out "D:/媒体" --last 3m
# 一键入口 wx_export 直接透传
python wx_export.py --group "群名" --outdir "D:/导出" --last 今天
python wx_export.py --user "晓东" --outdir "D:/导出" --last 7d --with-zstd 2>&1 | Out-Null
```


## v2.3 新增模块：朋友圈 / 收藏 / 专项消息 / 搜索 / 增量（均基于已解密库，不重复提密钥）

> 以下六个脚本都只读 `--dec` 指向的已解密库，**不需要再提密钥/解密**（直接复用 `~/.wxcache/decrypted`），时间过滤统一走 `media_common`。本机实测规模：朋友圈 395 条、收藏 91 条、公众号 414 个/20384 篇、转账 1585/红包 1296/小程序 6831、搜索全库约 120 万行 0.24s。

### 朋友圈导出（export_sns.py）

读 `sns/sns.db` 的 `SnsTimeLine`（content 是 XML `<SnsDataItem><TimelineObject>`），按时间倒序导出：正文 contentDesc、地点、图片/视频 URL、分享链接；`SnsMessage_tmp3` 按 `feed_id = tid` 关联出评论/点赞（折叠在 `<details>` 里）。昵称优先取 XML 内 `<LocalExtraInfo><nickname>`，回退 contact 表。

```bash
python scripts/export_sns.py --dec "<decrypted>" --out "朋友圈.md"
python scripts/export_sns.py --dec "<decrypted>" --out "朋友圈.md" --last 3m
```

### 收藏导出（export_favorite.py）

读 `favorite/favorite.db` 的 `fav_db_item`，按 type 分组渲染（本机实测：文字25/图片16/链接14/合并转发15/语音6/视频6/位置2/笔记3/文件1/小程序1/视频号2），开头有类型统计表。

```bash
python scripts/export_favorite.py --dec "<decrypted>" --out "收藏.md"
```

### 服务号 / 公众号文章（export_biz.py）

公众号推送单独存 `message/biz_message_0.db`（453 张 `Msg_<md5(gh_username)>` 分表），`message_content` 是 zstd 压缩 BLOB，解压后是 `<appmsg>` XML（title/des/url/category）。按公众号分组导出。

```bash
python scripts/export_biz.py --dec "<decrypted>" --out "公众号文章.md"
python scripts/export_biz.py --dec "<decrypted>" --session gh_68b976f584b5 --out "某号.md"
```

### 转账 / 红包 / 小程序分享（export_transfer.py）

跨 `message_0~8.db` 扫 `(local_type&255)=49` 的 appmsg，按 XML 子类型分流：

| 类型 | 识别特征 | 可解析字段 |
|---|---|---|
| 转账 | `<type>2000</type>` + `<wcpayinfo>` | feedesc=金额、pay_memo=备注、paysubtype(1发起/3已收)、payer/receiver、transferid |
| 红包 | `<type>2001</type>` | sendertitle/receivertitle=祝福语、nativeurl sendusername=发送人；**⚠️ 本地不存金额**（微信设计，金额只在账单） |
| 小程序 | `<weappinfo>` 且 appid 非空(type=33) | title、sourcedisplayname=小程序名、appid、username、iconurl |

```bash
python scripts/export_transfer.py --dec "<dec>" --out "转账红包小程序.md"            # 三类全导
python scripts/export_transfer.py --dec "<dec>" --kind transfer --out "转账.md"       # transfer/redpacket/miniapp
```

### 聊天搜索（search_messages.py）

**直接复用微信自带 FTS5 索引**（`message/message_fts.db`，约 120 万行，不用自建索引）。两个关键坑已踩平：

- 微信 FTS5 用自定义分词器 `MMFtsTokenizer`，Python 标准 sqlite3 报 `no such tokenizer` → 改读底层 `_content` 表 + LIKE 子串匹配（中文完全有效，全库 0.24s）。
- FTS 库自带 `name2id`（7570 行）与 contact.db 的 name2id 行号**不一致**，必须用 FTS 自己的 name2id 反查 session_id/sender_id，再到 contact 查昵称（实测命中率 99.8%）。

```bash
python scripts/search_messages.py --dec "<dec>" --keyword "微信"
python scripts/search_messages.py --dec "<dec>" --keyword "合同" --session "项目群" --last 7d
python scripts/search_messages.py --dec "<dec>" --keyword "转账" --limit 20 --out 搜索结果.md
# v2.4 新增 --type：按消息类型在结果侧过滤（不改 FTS 查询）
python scripts/search_messages.py --dec "<dec>" --keyword "面试" --type text     # 只看文本
python scripts/search_messages.py --dec "<dec>" --keyword "合同" --type link      # 只看链接/文件/小程序
```

`--type` 取值：`text/image/voice/video/sticker/location/link/file/system`。按 `local_type` 低位（`& 255`）在拿到 FTS 结果后筛，**不改动原有 FTS 查询逻辑**。
注意：① `system` 是整值哨兵（10000/10002，不取低位——10000&255=16 会错）；② `link`/`file` 低位同是 49（appmsg），低位无法再分，要区分"文件 vs 链接"需另解 appmsg XML；③ FTS 索引只收录文本类内容（文本/名片/位置/appmsg），图片/语音/视频/系统消息本身不在 FTS 里，按这些类型搜关键词自然为 0，属正常。

### 增量导出（export_incremental.py）

状态文件 `.wechat_export_state.json` 记录每个会话已导出的最大 `create_time`（+ last_local_id）。首次全量并初始化；后续只拉 `create_time > 上次` 的新消息追加到 Markdown 末尾。

```bash
python scripts/export_incremental.py --dec "<dec>" --session "群名" --out "导出/群名.md"
python scripts/export_incremental.py --dec "<dec>" --session "群名" --out "导出/群名.md" --full   # 强制全量
```

### 已知边界（如实记录）

- 朋友圈/收藏的图片视频是微信 CDN 网络地址（qpic.cn），本地库不含原始媒体，脚本只导出链接不下载。
- 红包金额本地库不存（`<feedesc>` 全空），属微信设计，需走「账单」导出。
- 转账 paysubtype 仅能区分 1=发起/3=已收；已退款/已过期本机样本未覆盖，标"未解析(paysubtype=N)"。
- 跨平台（macOS/Linux）适配可行性见 `docs/CROSS_PLATFORM.md`；wcdb-key-tool 三平台源码研究见 `docs/WCDB_KEY_TOOL_RESEARCH.md`。


## 聊天统计与群素材包（v2.4）

> 两个纯增量脚本，都只读 `--dec` 指向的已解密库，**不改现有任何导出脚本**（`search_messages.py` 仅加 `--type`，其余行为不变）。
> 发信人解析与分库合并逻辑直接复用 `export_group_md`（同目录 import），时间过滤统一走 `media_common`。
> 本机实测：某 Python 技术群近 30 天 26790 条，chat_stats 加载 2~3s，digest_source 产出 messages.json 约 8.4MB。

### 聊天统计台（chat_stats.py）

对指定一个群/联系人输出 Markdown 报告：消息总数（有效/系统两口径）、类型分布表、发言排行 Top N、按小时活跃分布、按日活跃分布（活跃天数/日均/峰值日）、时间跨度。

```bash
python scripts/chat_stats.py --dec "<dec>" --session "群名或联系人"
python scripts/chat_stats.py --dec "<dec>" --username "xxx@chatroom" --last 30d --top 15
python scripts/chat_stats.py --dec "<dec>" --session "晓东" --since 2026-08-01 --until 2026-09-01
python scripts/chat_stats.py --dec "<dec>" --session "群名" --out "统计.md"          # 写文件而非 stdout
python scripts/chat_stats.py --dec "<dec>" --session "群名" --no-zstd              # 跳过 zstd 解压（快；发信人仅用本库 Name2Id）
```

- `--session` 优先当群名（只在 `@chatroom` 里找），没命中再放宽到联系人（私聊）；`--username` 精确指定。
- 类型标签按 `local_type` 低位归类：`1` 文本/`3` 图片/`34` 语音/`43` 视频/`47` 表情/`48` 位置/`49` 链接文件小程序；`10000/10002` 整值哨兵（系统/撤回，不取低位）；`266287972401` 拍一拍、`244813135921` 复合按整值特判。
- 排行只统计有效消息（系统/撤回/拍一拍无发信人，不参与）。

### 群素材包（digest_source.py）

对指定群 + 时间范围，在 `--outdir/<会话名>/sources/` 下生成一套机器可读 + 人可读素材包，供下游知识库/群刊/AI 提炼消费：

```bash
python scripts/digest_source.py --dec "<dec>" --session "群名" --outdir "D:/素材包"
python scripts/digest_source.py --dec "<dec>" --session "群名" --outdir "D:/素材包" --last 30d
python scripts/digest_source.py --dec "<dec>" --username "xxx@chatroom" --outdir "D:/素材包" --since 2026-08-01
```

产出三件：

| 文件 | 内容 |
|---|---|
| `sources/messages.json` | 机器可读：每条消息 `ts/time/sender/sender_disp/type_low/type/is_system/content`；正文截断 200 字，大群不把整库塞进单文件 |
| `sources/stats.json` | 与 chat_stats 同源的统计（总数/类型分布/发言排行/24h 分布/每日分布/跨度） |
| `sources/material.md` | 人可读素材稿：话题概述、发言排行、关键消息摘录（有效文本里正文最长的 N 条） |

- 与 chat_stats 共用 `load_messages`/`compute_stats`，统计口径同源。
- `--excerpts N` 控制关键消息摘录条数（默认 15）；`--no-zstd` 同 chat_stats。


## 跨会话批量导出（v2.4）

> 场景：「帮我梳理一下昨天所有聊天记录，总结一下有哪些事项」——需要跨**全部**会话（群 `@chatroom` + 私聊 wxid）一次性按时间窗捞消息，而不是一次只导一个群/会话。纯增量脚本 `export_all_sessions.py`，不改任何现有导出脚本。

### 架构：从消息出发，SQL 时间窗直查，会话只做命名

与 `export_group_md`「先定位一个群再找分表」相反，本脚本**不枚举会话去逐个探测**，而是：

1. 只遍历 `message/message_<N>.db`（N 为数字 0..8；`biz_message_0.db` / `media_*.db` / `message_fts.db` / `message_resource.db` / `weclaw.db` 按正则 `^message_\d+\.db$` 一律排除）。
2. 每个分库**只开一次连接**：一次性 `SELECT name FROM sqlite_master ... LIKE 'Msg_%'` 拿到该库全部表名，一次性读出该库 `Name2Id`（rid→user_name，局部于库）。
3. 对每张 `Msg_<md5(username)>` 表直接跑带时间窗的 SQL：
   `SELECT ... FROM "Msg_<hash>" WHERE create_time >= ? AND create_time <= ?`。
   昨天/近窗没消息的会话 SQL 自然 0 命中，**根本不进结果**——无需逐个会话探测。
4. 命中按表名 hash 分组、组内按 `(create_time, sort_seq)` 正序；跨库分片沿用 `export_group_md` 已验证的 `(时间, real_sender_id, 内容哈希)` 去重（只与前面的库比，不误删同库内同秒同人不同文）。
5. 最后一次性 hash→username→昵称：`contact.db` 一次查 `username/nick_name/remark` 建字典，再 `md5(username)` 反查表名 hash；`SessionTable`（session.db）仅作「枚举会话总数/跳过清单」的真实会话 universe，**绝不用于驱动查询**。

发信人解析沿用 `export_group_md` 两级定案（踩坑#20）：内容前缀 `<发信ン>:\n`（群内他人消息真身）> 本库 `Name2Id` 解析 `real_sender_id`（无前缀=自己发，或私聊对方）。

### 用法

```bash
# 一键入口（推荐，走 dec 缓存；--outdir 必填）
python wx_export.py --all-sessions --last 昨天 --outdir "D:/导出"
#   产出 <outdir>/全部会话汇总.md + 全部会话汇总_底座.db + 全部会话汇总_底座.json

# 直接跑（调试）
python scripts/export_all_sessions.py --dec "<dec>" --last 昨天 --out "汇总.md"
python scripts/export_all_sessions.py --dec "<dec>" --last 7d --out "汇总.md" --sqlite 底座.db --json 底座.json
python scripts/export_all_sessions.py --dec "<dec>" --last all --out "全量.md" --no-groups          # 只要私聊
python scripts/export_all_sessions.py --dec "<dec>" --since 2026-09-01 --until 2026-09-16 --out "汇总.md"
```

参数：`--include-groups/--no-groups`、`--include-private/--no-private`（默认全开）、`--max-text` 单条正文截断（默认 300 字防爆体积）、`--no-zstd` 跳解压；时间窗复用 `--last/--since/--until`（昨天/今天/近N天/周/月/年/all，中英）。

输出三件：

| 产物 | 内容 |
|---|---|
| `汇总.md` | 开头一段统计（枚举会话数/有消息会话/跳过数/消息总数）；每个会话一节（标题=昵称·群/私聊·条数·首末时间），消息按时间正序带发信人昵称；会话按条数降序排 |
| `底座.db`（可选 `--sqlite`） | 两表：`sessions(username,display_name,kind,msg_count,first_time,last_time)` + `messages(session_username,create_time,sender,sender_display,local_type,is_system,content)`，供上层 AI 总结事项 |
| `底座.json`（可选 `--json`） | 与 SQLite 同源的 JSON，供程序读 |

结尾打印：枚举会话总数、有消息会话数、跳过会话数及样例、总消息数、逐分库扫描统计、耗时。

> 本机实测（2026-09-17，`--last 昨天`）：枚举会话 1454（SessionTable 真实会话），时间窗内 42 个会话有消息、共 3832 条（有效 3785 / 系统 47），9 个分库仅 `message_1.db` 命中，耗时约 15s。抽样与 `export_group_md` 同窗口比对：某群 1556=1556、某私聊 49=49，条数一致（差 0）。


## 全天跨会话梳理包（v2.5，export_day_digest.py）

> 场景：「帮我梳理昨天的聊天」要的是**一天一梳的阅读包**——逐会话一个文件、按小时分节、
> 引用/转账/红包/小程序细分渲染、一份总览带文件链接。与 `export_all_sessions.py`（v2.4）**互补**：
> all-sessions 产出**单文件汇总 + SQLite/JSON 底座**（喂给程序/上层 AI 做事项总结）；
> day-digest 产出**多文件梳理包**（直接给人读，或供 AI 逐会话细读）。
> 两者同窗实测条数一致（42 会话 / 3832 条），可互为交叉验证。

### 用法

```bash
# 一键入口（推荐，走 dec 缓存；--outdir 必填）
python wx_export.py --digest yesterday --outdir "D:/导出/昨天"
python wx_export.py --digest 2026-09-16 --outdir "D:/导出" --merge-under 100 --cap 200

# 直接跑（调试）
python scripts/export_day_digest.py --dec "<dec>" --date yesterday --outdir "D:/导出"
```

参数：`--date`（YYYY-MM-DD，或 today/今天、yesterday/昨天；**注意口径是 `--date`，不是 `--last`**）、
`--cap`（单条截断，默认 150 字）、`--merge-under N`（少于 N 条的会话并入 `_其余会话合集.md`，默认关）、
`--prune`（⚠️ 调试专用，见踩坑#29，正式数据勿用）。

输出三件：

| 产物 | 内容 |
|---|---|
| `_总览.md` | 总量/分类统计 + 会话清单（条数 + 文件链接） |
| `NNN_<会话名>.md` | 大会话逐个一文件，按小时分节，头部带参与人数/发言最多 |
| `_其余会话合集.md` | `--merge-under N` 时小会话合并成册 |

分类桶：文本/媒体/引用/链接文件/小程序/转账红包/系统/其他——引用带被引用人+内容摘录
（refermsg displayname+content），转账/红包带金额与备注（wcpayinfo，wctype 2000/2001），
小程序带标题（weappinfo / `<type>` 33/36）。md5→username 映射从 contact.db 全量构建
（不硬编码），映射外的孤儿分表计数上报不静默丢弃。


## 只读实时消息监听（v2.6，watch_messages.py）

这是对上游 `Listener` 思路的本地库实现，适合需要“新消息一到就交给脚本/AI”的场景。它只读 `--dec` 下的解密库，不连接 UIA、不发送消息、不修改微信文件；轮询时每个 `message_*.db` 只读打开一次，自动发现后续新增分片。

```bash
# 指定群/联系人持续监听（会话可重复写多个 --session）
python scripts/watch_messages.py --dec "<dec>" --session "群名" \
  --state "D:/监听/listener_watermark.json"

# 全部会话首次从时间点回放一轮；JSONL 可直接喂给下游程序
python scripts/watch_messages.py --dec "<dec>" --all --since 2026-09-16 \
  --once --format jsonl --out "D:/监听/回放.jsonl" \
  --state "D:/监听/listener_watermark.json"
```

行为约定：首次运行未指定 `--since` 时建立各分片当前尾部基线，不回放历史；显式 `--since` 才从该时间点开始。每条消息的水位由 `(create_time, sort_seq, local_id)` 组成，回调成功或重试耗尽后才落盘推进，因此是至少一次投递；进程在输出后立即中断时，重启可能重复最后一条。无 `--state` 时不跨重启保存水位。`wx_export.py --watch/--watch-all` 提供同样能力的一键入口，并把输出/水位放入 `--outdir`。


## 按发送者精确直查（v2.7，export_sender_messages.py）

> 场景：只要某个发送者（通常是自己）发的消息——做个人发言画像 / 单方审计 / 发言统计，
> 别全量导出再在结果里筛。SQL 层直接 `WHERE real_sender_id IN (rids)` 精准取数。

### 架构：从消息出发，按 rid 过滤直查，会话只做命名（与 all-sessions 同向、更省）

1. 只遍历 message/message_<N>.db（N 为数字 0..8；biz_message/media_*/message_fts 同前缀库一律排除）。
2. 每个分库只开【一次】连接：一次列出该库全部 `Msg_` 表名，一次读出该库 Name2Id。
3. 对每张 `Msg_<md5(username)>` 表直接跑 SQL：
   `WHERE real_sender_id IN (目标rids) [AND create_time 范围]`——没命中自然 0 行，不进结果。
4. 跨库去重沿用 (时间, rid, 内容哈希) 键，只与前面的库比。

### 发送者身份的三条不变量（务必理解，否则会导错人）

1. **rid 每库各自为政**：同一 wxid 在 message_0..8.db 的 Name2Id 里 rowid 可能不同，
   必须逐库读该库 Name2Id 解析目标 wxid 的 rid，绝不可拿 A 库的 rid 去 B 库过滤。
2. **内容前缀 > 本库 Name2Id**：群内他人消息真身是内容前缀 `<发信人>:\n`；目标发送者自己发的消息
   理论上【不应该】带他人前缀。出现开头前缀 = 疑似误配，`--verify` 会把它们收集进报告。
3. **跨库去重**：同一消息可能被多分库重复收录，按 (create_time, real_sender_id, 内容哈希)
   与前面库比对去重。

### 用法

```bash
# 一键入口（推荐，走 dec 缓存；--outdir 必填）：
#   产出 <outdir>/按发送者直查_底座.db（sessions / messages / meta / verify_suspicious 四表）
python scripts/wx_export.py --sender-messages <本人wxid> --outdir "D:/画像"

# 直接跑（调试；多个 --sender 合并为"任一命中"）
python scripts/export_sender_messages.py --dec "<dec>" --sender <本人wxid> --out 我的发言.db
python scripts/export_sender_messages.py --dec "<dec>" --sender <wxid> --last 7d --out 近7天.md
python scripts/export_sender_messages.py --dec "<dec>" --sender <wxid> --session "项目群" --out 项目群.db
python scripts/export_sender_messages.py --dec "<dec>" --sender <wxid> --verify --out 校验.json
```

时间过滤三件套 `--since/--until/--last` 与全局一致；`--session` 接受显示名片段（唯一匹配）；
`--verify` 收集"开头带他人前缀"的消息（疑似误配）写入输出（SQLite 的 verify_suspicious 表 / JSON 的 verify_rows 段）。

### 实测（本机 Windows / 微信 4.1.13）

全库约 142 万行、跨 11 个 message_<N>.db、1475 张 Msg_ 表，直查本人 142,434 条有效消息约 **2.6s**
（含 zstd 解压与写库）。消息开头带他人真身前缀者 **0 条**——rid 直查无真身级误配。
曾踩坑：循环内每次调用 split_prefix 时重建 2.6 万元素 known_users 集合，14 万条消息累积成 196s；
改为每库构建一次后 2.6s（约 75 倍）。export_all_sessions 已同步此优化。


## 跨平台（macOS / Linux，v2.4 已落地为代码）

> 路线图结论见 `docs/CROSS_PLATFORM.md`。本节说"代码已怎么接、平台怎么分、哪些真跑过哪些没"。
> 本机是 **Windows**：所有 darwin/linux 代码只做了语法/import/逻辑走查，**未在 mac/linux 真机跑过**，
> 严禁对外声称"mac 真机已通过"。

### 平台分支表（按 `sys.platform` 自动切，上层零改动）

| 能力 | win32 | darwin (macOS) | linux |
|------|-------|----------------|-------|
| AES-256-CBC（整库） | bcrypt.dll CNG | CommonCrypto `CCCrypt` | OpenSSL EVP（libcrypto） |
| AES-128-ECB（图片区） | bcrypt.dll CNG | CommonCrypto `CCCrypt`（ECB 模式位） | OpenSSL EVP（`EVP_aes_128_ecb`） |
| 数据目录探测 | ini → 数据根 → `xwechat_files/<wxid>/db_storage` | `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage` | `~/.local/share/com.tencent.wechat/xwechat_files/<wxid>/db_storage` |
| 找微信进程 | `tasklist Weixin.exe` | `pgrep -x WeChat` | 遍历 `/proc/*/exe` 找结尾 `/wechat` |
| 进程内存枚举/读 | `VirtualQueryEx`+`ReadProcessMemory` | `mach_vm_region`+`mach_vm_read` | `/proc/<pid>/maps`+`/proc/<pid>/mem` |
| 取数据库密钥 | `extract_keys_413.py`（XOR blob 破解，本机实测） | `extract_keys_macos.py`（内存扫 raw key / LLDB 断 `CCKeyDerivationPBKDF`，**可选 Frida 备选**） | `extract_keys_linux.py`（ELF 锚点串 + GDB，仅 x86_64） |
| 图片密钥内存扫 | `media_common` kernel32 三件套 | 同上（task_for_pid 版） | 同上（/proc 版） |
| WXGF 未查看原图 | `VoipEngine.dll` → jpg | 在 `WeChat.app/Contents/Frameworks` 找 dylib 调 `wxam_dec_wxam2pic_5`（【推断，未真机】） | **降级**：官方 Linux 不附带解码库，未查看原图不可用，已查看的明文图/视频不受影响 |
| 语音 SILK→WAV | pysilk | `pip install silk-python`（代码无需改） | `pip install silk-python`（代码无需改） |

### 前置条件（macOS）
```
sudo codesign --force --deep --sign - /Applications/WeChat.app   # 去 Hardened Runtime（微信更新后可能要重签）
xcode-select --install                                          # 提供 lldb
# 以 root 运行：task_for_pid / lldb attach 需要
sudo python3 scripts/extract_keys_macos.py extract
# 首次抓 passphrase：微信内 退出登录 → 重新登录 触发
```
可选增强（非必装）：`pip install frida frida-tools`，装了优先用 Frida hook `CCKeyDerivationPBKDF`（参考 yichen-wechat-local-vault 思路），没装或失败自动回落 LLDB。

### 前置条件（Linux，仅 x86_64）
```
sudo apt install gdb libssl-dev
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope   # 或直接 sudo 跑
sudo python3 scripts/extract_keys_linux.py extract      # 首次需退出重登触发
```

### 验证级别（务必如实标注）
- 【已验证 = Windows 真机回归】：`aes_backend` win32 后端（含 FIPS-197 AES-128-ECB 已知答案向量、真实解密库 search/sns/favorite 回归）、`media_common` 在 win32 走原 kernel32/bcrypt 路径、`wx_export.detect_db_dir()` 在 Windows 正常定位。
- 【代码级验证，未真机 = mac/linux】：`aes_backend` 的 darwin(CCCrypt)/linux(EVP) 后端、`extract_keys_macos.py`、`extract_keys_linux.py`、`media_common` 的 darwin/linux 内存分支、WXGF macOS dylib 定位。均只在 Windows 上 `py_compile` + import + 逻辑走查，未真机。
- WXGF macOS dylib：【推断，未真机】——微信 4.x 跨平台共用 WCDB/图片解码代码，推断存在同名导出符号，但未在 mac 真机核对具体 dylib 名。
- Linux ARM：不支持（ELF 分析硬校验 x86_64）。


## 踩坑实录（按遇到顺序）

1. **`pip install wechat-cli` 是假的**：PyPI（含清华镜像）无此包（`from versions: none`），疑似已下架。技能文档里的安装指令不可信，上层 `sessions/history/export` 命令全是空中楼阁——一切靠自带脚本 + 自写导出。
2. **技能文档里的"本机环境速查"是别人机器的**（`C:/Users/Administrator/...`、`E:\微信\...`）。遇到任何硬编码路径先验证是否存在于当前机器。
3. **4.1.13 的密钥形态变了（核心坑）**：不再是明文 `x'...'` 字符串，而是 **XOR 混淆后的 99B 定长 blob**（magic 前 2 字节 `aa e0`）。表现：runtime scan 找到几十个 candidate 但 HMAC 全不过；正则找 hex 串为 0 条。
   破解原理：99B = 2(`x'`) + 96(hex) + 1(`'`)，混淆是**逐位置 XOR**。对每个字节位置 i，求 k[i] 使得**所有去重 blob** 在该位置的解密值都落在 hex 字符集（约束极强，29 个样本基本唯一确定 k[i]）；首尾用 `'x'`、`'`、`'` 钉死。少数模糊位置枚举组合，用「解出串的后 32 hex 必须等于某库真实 salt + HMAC 校验」双重验证筛正确组合。
4. **4.1.13 仍是 key 直用形态**：解出的 64 hex 直接当 AES key 用（HMAC 直接过），**不需要 PBKDF2**。GitHub README 说 4.1+ 要 PBKDF2 是早期 4.1.0~4.1.11 的路线；别浪费半小时跑 PBKDF2 兜底扫描。
5. **blob 有大量重复**：dump 出 225 个去重后只有 29 个。先去重再分析，否则破解约束计算和输出全被噪音淹没。
6. **管理员权限不是必须**：微信与当前用户同身份时，`OpenProcess+ReadProcessMemory` 非管理员即可（实测 5 个 Weixin 进程全读成功）。别一上来折腾提权。
7. **`message` 库里的 `Name2Id` 表不是 id 映射**（只有 user_name/is_session 两列）。真正的 sender 映射在 `contact.db` 的 `name2id` 表：**rowid 就是消息的 real_sender_id**，username 列是 wxid。拿错表会得到 0 条映射、发送者全是"未知ID"。
8. **失效排查顺序**（未来微信又更新时）：① blob dump=0 → Config.Cipher 对象链路偏移变了（`node-0x10 → +0x28 → +0x88 → data_ptr`），需重新逆向；② blob 非 99B/magic 变了 → 混淆方案换版，重走"逐位置约束"需先确认明文仍是 hex 字符集；③ HMAC 不过但 salt 匹配 → key 可能变 PBKDF2 形态，改用 passphrase+派生。任何一步先看 GitHub 仓库有没有新 commit/issue。
9. **密钥会随微信重启/更新失效**（每次登录重新派生）。数据库文件不动则 salt 不变，但 key 换了就要重跑 Step 1。
10. **zstd 压缩判定**：`message_content` 为 bytes 且以 `\x28\xb5\x2f\xfd` 开头，或 `WCDB_CT_message_content=4`。压缩消息多是引用/文件/小程序/合并转发（XML），明文普通聊天不压缩。
11. **`local_type` 会出现大数值**（如 244813135921，带 flag 的复合类型），按"富文本"处理即可，别当未知错误。
12. **导出大表用 `sort_seq` 排序**（不是 create_time——同秒消息靠它保序）。
13. **⚠️ 必须按 magic 前缀过滤 blob，且求解用"投票制"而非"全票制"**（最隐蔽的坑，曾导致首次 23/23 成功、脚本化后 0/23）：
    - 内存里会 dump 到**同长度(99B)但不同来源**的 blob。实测一批 30 个里混了 1 个前 2 字节是 `406f` 的冒牌货，其余 29 个是 `aae0`。**按长度分组分不开**，必须再按前 2 字节取众数过滤。
    - 逐位置求 XOR key 时，**不要要求"所有 blob 该位置都落 hex 字符集"**（全票制）。混入 1 个噪声 → 某位置凑不出全票 → 整变体被判无解 → 全盘 0 命中。正确做法是**投票制**：每个位置取"让最多 blob 落 hex 集"的 k；个别位置本来就无解时取 0 继续跑，靠后面的 salt 硬比对 + HMAC 兜底筛错。
    - 教训（通用）：把探索期的一次性脚本封装成通用脚本时，**别顺手把容错收紧成"一票否决"**。探索期数据干净所以没暴露，真实环境有噪声立刻崩。封装后必须在**含噪声的原始数据**上回归验证，不能只在干净数据上验过就交付。

14. **装 zstandard 必须用 `python -m pip`，别用 venv 里的 `pip.exe`**：实测直接调 `Scripts/pip.exe install zstandard` 会**静默无输出、exit 0 但根本没装上**（import 直接 ModuleNotFoundError）。改用 `python -m pip install zstandard` 才真正装进去并可用（任意装有该包的 Python 3.10+ 环境即可）。

15. **raw string 里写反斜杠正则极易错（实测踩到）**：`r"[A-Za-z]:\\\\[^\\r\\n]+"` 在 raw string 里是 4 个反斜杠 → 正则语义变成"匹配**两个**字面反斜杠"，永远命中不了 `X:\微信数据`。表现：自动探测报"未在 ini 中找到数据根目录"，但 ini 里明明有。**规避：不要用正则匹配 Windows 路径**，改逐行解析 + `s[1] == ":" and os.path.isdir(s)` 判断（见 `wx_export.py: detect_db_dir`）。
16. **`wechat-cli` 技能是死路，别被带偏**：PyPI 无该包（踩坑#1），其 `sessions/history/export` 全是空中楼阁，且文档里的"本机环境速查"是别人机器的路径（踩坑#2）。**用户若 `@skill:wechat-cli`，应直接切到本技能**，否则会白绕好几轮（本次实测就绕了一轮）。
17. **缓存是最大效率杠杆，也是最敏感产物**：缓存密钥+解密库后，导出下一个群从 **~6min 降到 0.46s**（实测，Step1/2 全跳过）。但缓存 = **明文全量聊天记录 + 数据库密钥**，约 0.8GB。收尾原则：要么 `--purge` 清掉，要么**显式告知用户缓存路径与体量**由其决定；别默认留着，也别不问就删。

18. **消息内容前缀 `<id>:` 不等于发送者 wxid（最隐蔽的坑）**：原脚本按"发送者 wxid + `:`"精确剥离前缀，导致富文本（链接/图片/小程序）**整坨 XML 吐进 Markdown**。实测该前缀有**三种形态**，且**常常不是 `real_sender_id` 映射出的 wxid** —— 某群内容是业务号 `1234567890@openim`，而 sender 是 `wxid_xxxx`：
    - `wxid_xxxx:`
    - `xxx@openim` / `xxx@chatroom`（业务号、群号）
    - 老式微信号：`userabc:`、`user123:`（正则 `[A-Za-z][A-Za-z0-9_-]{5,19}`）
    正解：按"看起来像 ID"的正则统一剥离（见 `ID_PREFIX_RE`；字符集不含中文，所以不会误伤"各位:\n"这类正常文本），**不要**用发送者 wxid 精确匹配。修完残留原始 XML **73/266 → 0/0**，某读书群文件 4179→2409 行（去掉 XML 膨胀）。
    - **元教训（比这条坑本身更重要）**：我先后猜了两次都错（"前缀就是 wxid"、"是 `\r\n` 导致判定失效"），白改两轮。正确做法是**先花 20 行诊断脚本直接 dump 原始 `message_content` + `real_sender_id` 看一眼**，一眼看清前缀到底长啥样。猜三轮不如查一次。

19. **一个群的消息可能被拆存到多个 `message_N.db`（最致命：会静默漏掉大半）**：微信 4.x 会把同一群的 `Msg_<md5>` 分表**同时放在多个 message 库**里，各存一段时间且**首尾完美衔接**。旧代码定位分表时 `for fn` 循环命中后没有 `break`，`if tbl: break` 写在 fn 循环**之后** → **最后一个命中的库覆盖前面的**，且从不合并 → 只导出其中一段。
    - 实测：某群 `message_1.db` 2832 条（04-28→05-11）+ `message_0.db` 19385 条（05-11 15:39→09-08）→ 应得 22217，实得 **2832，漏 87%**，**不报错、不警告**。
    - 排查手法：用 `os.walk` 钩子插进脚本打印真实遍历顺序——**纯读代码会误判**（极易以为"取第一个命中"）。
    - 正解：收集**所有**含该分表的库 → 合并 → 按 `create_time` 排序（不同库的 `sort_seq` 不具可比性）。
    - **去重只能跨库做**：按 `(create_time, real_sender_id, md5(内容))`，且只与"前面的库"比对。若做全库去重，会误删同秒同人的不同消息（实测每群少 1~4 条）。
    - **必加完整性自检**：打印时间跨度 + 命中库数 + 最新消息距今小时数（>48h 报警），否则漏数据永远看不见。
    - 影响面参考：实测某账号 475 群中，被拆分的多为**跨度长/久未清理**的群（如 2 年跨度群分布在 3 个库）。

20. **发信人识别终极规则（"张冠李戴"根治，用户实测抓出的 bug）**：`real_sender_id` 是【每个 message 库局部】的序号，**不是** `contact.db name2id` 的全局 rowid。旧代码拿全局 rowid 兜底 → 本群 rid=4 撞上全局 rowid 4=`floatbottle`（漂流瓶）、rid=9 撞上 `mphelper`（公众平台安全助手）——几百条真人消息全被安到系统账号头上。
    - **权威解析两级定案**：① 他人消息内容必带 `"<username>:\n"` 前缀 = 发信人真身（实测 21469 条前缀 vs 本库 Name2Id **0 冲突**）；② 无前缀消息 = **自己发的**（微信不给自己加前缀），用【消息所在库】的 `Name2Id` 表（rowid→user_name）解析。前缀与 Name2Id 双向互证。
    - **rid 空间按库独立**：同一个人（自己）在 message_0.db 是 rid=4、在 message_1.db 是 rid=9——跨库合并的 rid 映射天然不可靠，必须逐库解析（导出脚本收集行时就带上本库解析结果）。
    - **全局 name2id 的 rowid 1~9 是雷区**：notifymessage/qqmail/qmessage/floatbottle/medianote/weixin/mphelper 等系统账号占据小号，任何"拿全局 rowid 解析消息 rid"的兜底迟早撞车。**全局 id2name 只许用于成员核验**（`chatroom_member.member_id` = 全局 rowid，这个对应关系才是对的）。
    - **验证手段（引用消息反查，比猜测可靠）**：引用消息 XML 的 `<refermsg><svrid>`（原消息 server_id）+ `<chatusr>`（被引用人真身）可反查任意无前缀消息的发送者——实测 2061 条 chatusr 与内容前缀完全一致（方法自证），据此锁定 rid=4/9 都是自己。**拍一拍/撤回等系统事件归属固定 rid（如 121），Name2Id 留空**；该槽位下的媒体消息显示"未知ID_x"是数据属性，不是 bug。
    - **必加成员核验自检**：发送者集合应 ⊆ 本群成员名单（chatroom_member）。名单外发送者多为**已退群成员**（正常——成员表只存当前名单，实测 133 天老群 115 个发送者中 25 人已退群、发言占 1.7%）；**过半**不在名单才报警（映射 bug 时会归零）。

21. **SQL UNIQUE 去重键比消息身份粗，会静默丢消息（结构化输出首版踩到，实测丢 18/22217）**：给 messages 表加 `UNIQUE(room_id, ts, sender, local_type, content)` 防"重复导出翻倍"，但系统消息常出现**同秒、同人、同内容**（连续几条"[系统]"），被当成重复 `INSERT OR IGNORE` 掉——不报错、总数对不上才发现。**正解：放弃 UNIQUE，改"整群重写"（DELETE 该群再 INSERT）**——天然幂等、零误删。教训：**去重键必须精确到消息身份；身份不唯一的场景，用"替换式写入"而不是"忽略式写入"**。验证手段：入库总数必须与解析总数逐次相等（22217=22217），重跑一次数不变。

23. **图片密钥 ≠ 数据库密钥，且可由登录态 code 派生（媒体解密核心突破）**：V2 图片的 AES 密钥不是
    从 Config.Cipher 对象拿（那是 DB 密钥），而是 `md5(code+wxid)[:16]`，xor = code & 0xFF。
    code 是账号级登录态整数，**常驻微信进程内存**（实测主进程数百处 4 字节副本）——直接扫
    4 字节 LE 窗口（低字节 == 从 JPEG 尾部 `FF D9` 推断的 XOR 预过滤，1/256 提速 ~200 倍），
    逐候选派生 + 模板密文 AES 验证即命中。**这是"初始化全自动"的关键**：无需用户打开图片。
    - 踩坑子项：**不能只取出现次数最多的候选**（内存里低字节匹配的无关整数更多，实测
      566534 个候选中真实 code 排在第 8 位左右）；必须按次数降序逐个派生验证，上限 5 万。
    - 验证 oracle：V2 文件头部首个 AES 块解密后应为 jpg/png 头。没有模板就什么都验不了，
      所以先扫 `attach/*/*/Img/*.dat` 找模板（模板要 `_t.dat` 缩略图，普通 `.dat` 可能是 V1）。
24. **wxid 派生用短名（去掉设备后缀）**：账号目录名形如 `wxid_xxx_8bb3`，但密钥派生用的
    wxid 是 `wxid_xxx`（无 `_8bb3`）。脚本自动尝试两种形态，用模板验证选出正确者。
    别硬编码目录名当派生输入。
25. **V2 头部是 15 字节不是 16**：`070856320807`(6B) + aes_size(LE u32) + xor_size(LE u32) + pad(1B)。
    aes_size 需对齐到 16 的倍数才是 AES 区长度；PKCS7 unpad 失败（非 PKCS7 填充）时原样保留尾部。
26. **WXGF 转码必须用微信自带的 VoipEngine.dll**：`wxam_dec_wxam2pic_5` 导出函数，mode 0/3 循环，
    输出上限 52MB。DLL 从运行中 Weixin.exe 的安装目录递归探测（不写死路径）。找不到 DLL 时
    WXGF 文件转码失败——提示安装微信即可，不是解密逻辑问题。
27. **PowerShell 通配符坑（纯排查误导）**：目录名含 `[`（如 `#[MOTHER]`）时
    `Get-ChildItem -Directory | % FullName` 显示 0 个文件，是 `[` 被当通配符，文件其实都在。
    用 Python os.listdir 复查，别据此误判导出失败。
28. **语音不落盘是假象，数据在 VoiceInfo 表（语音导出的核心突破）**：VoiceTemp 目录全空、
    全盘无 .silk/.amr 不代表没语音——微信把语音 BLOB 存在 `message/media_*.db` 的
    `VoiceInfo` 表（svr_id ↔ 消息 server_id）。先查库再下结论，别扫文件系统。
    - 解码链路（全 Python 原生）：`pip install silk-python` → `pysilk` 直接吃
      `0x02#!SILK_V3` 头 + 2B 帧长前缀（内部处理，无需自己拆帧）→ 24kHz WAV。
    - 备选：微信 `VoipEngine.dll` 的 `SKP_Silk_SDK_*`（ctypes 可解，但绑微信版本）；
      `silk_v3_decoder.exe`（kn007 预编译，可解但引入第三方 exe）。**pysilk 最优**。
    - whisper 本地转写（faster-whisper）在 Python 3.14 上 ctranslate2 加载模型崩溃
      （0xC0000005）——若走本地转写建议 Python ≤3.13，或直接用 ASR 接口。

22. **「天数」口径不统一会跨技能差 1（digest 群刊实战发现）**：导出完整性日志用 `(t1-t0)/86400`（差值天，不含首尾），群刊页面用 `date 差 +1`（含首尾的覆盖天）——同一群一个报 42 天一个报 43 天，用户对账必懵。**统一口径：跨度天数 = 含首尾覆盖天（秒差/86400 + 1，或 date 差 +1）**，页面与日志永远同数。教训：跨脚本/跨技能的"同义数字"要同源定义，发现差 1 先查口径而非数据。

29. **SessionTable 懒落盘，last_timestamp 会滞后（跨会话按天导出曾静默丢 4 会话/633 条）**：
    `session.db` 的 `SessionTable`（⚠️ 表名不是 `session`，猜错直接 `no such table`）记录每个会话的
    `last_timestamp`，但它是**懒落盘**的——实测某群消息库已写到 09-17 00:46，SessionTable 里
    last 还停在 09-15 23:18。拿它当"今天/昨天有没有新消息"的**剪枝依据会静默剪掉整天消息**
    （实测全扫 42 会话/3832 条 vs 剪枝 38/3199，不报错不警告）。
    - **正解**：跨会话按时间捞消息一律**全扫 `Msg_%` 分表 + SQL WHERE 时间窗**（`Msg_` 表无
      `create_time` 索引，过滤必须放 SQL 里，逐行 Python 判会慢死；11 库全扫实测 ~15s，可接受）。
      `SessionTable` 只许做"会话总数枚举/命名参考"，**绝不用于驱动查询或剪枝**。
      `export_day_digest.py` 的 `--prune` 仅留作调试开关并全程红字警告。
    - **同场加映两个小坑**（同一次开发踩到）：
      ① appmsg 的 `<type>` 子类型**不能锚定开头提取**——XML 里 `<title>` 排在 `<type>` 之前，
      `re.match(r"<type>")` 永远空手，必须 `re.search(r"<type>(\d+)</type>")` 任意位置；
      ② `244813135921`（57<<32|49）是**引用**复合类型，必须落 49 富文本分支解析 refermsg，
      绝不能跟拍一拍（266287972401）一起进系统桶——按整值特判时两类大数要分开。
    - **回归方式**：同窗双跑 `--digest` 与 `--all-sessions --last` 对总数；再比对单群
      `export_group_md` 抽样条数（本次三方一致：42/3832）。

## 验证方式（改脚本后必跑）

```bash
# 回归用例：拿"含冒牌 blob"的 dump 目录跑，必须仍能 23/23
python extract_keys_413.py --db-dir "...\db_storage" \
  --dump-dir "<沙盒>\config_dump(含噪声)" --out "<沙盒>\all_keys.json"
# 期望日志：
#   [i] 分组(2类): [('aae0', 29), ('406f', 1)] -> 采用 aae0 x29
#   filtered blobs: 29 条, len=99
#   === DONE ~30s: 23/23 ===
```

**完整性回归（防踩坑 #19 复发）**——导出后必须核对这三项：

| 检查项 | 合格标准 |
|---|---|
| `定位: N 个库含该群分表` | N>1 说明该群被拆分，**必须全部合并**（旧版只取一个，静默漏数据） |
| `时间跨度: 起 -> 止 (X 天 / Y 条)` | 止点应接近当天（差 2~3 小时属正常同步延迟） |
| `[!] 最新消息距今 N 小时` | >48 小时才报警，提示可能漏分库或本地未同步 |
| `成员核验: 发送者 X 人中 Y 人在本群成员名单` | 名单外多为已退群成员（正常）；**过半**不在名单 → 发信人映射有问题（踩坑#20） |
| `消息口径对账: 共 N 条 = 有效 X + 系统 Y` | `X` 应等于统计底座 `groups.msg_count`，也等于群刊展示的「有效消息」数；三者不一致先查这行（差值是系统/撤回/拍一拍） |
| 导出 MD 的消息类型标签 | 已知类型（文本/图片/语音/视频/表情/位置/链接/系统/撤回/拍一拍/复合/名片等）显示中文；未知类型统一显示「应用消息」或「微信消息」，**不再出现 `类型{数字}` 裸码**（审计 F 补全） |
| 媒体导出日志 `成功 N/M（失败 X，WXGF 转码 Y）` | 失败应为 0；WXGF 失败需检查 VoipEngine.dll 是否存在（踩坑#26） |
| 语音导出日志 `语音消息 N，VoiceInfo 命中 H（未命中 M），WAV W` | 命中率应 ~99%+（未命中为过期/撤回）；WAV 用播放器抽查，RIFF/WAVE 头完整（踩坑#28） |
| 语音时间线 | `语音时间线.csv` 与 `voice_map.json` 已生成；Markdown 导出带 `--voice-map` 时 `[语音]` 行有 🎤 WAV 路径，且与行首时间同源 |
| 抽查解密图片 | 随机挑一张用看图工具打开，应为正常照片（非乱码/黑块） |
| 全天梳理包 `--digest`（v2.5） | 日志 `会话 N 个 / 消息 M 条` 与 `--all-sessions --last <同日>` 总数一致（互为交叉验证）；`_总览.md` 分类桶之和 = M；日志应为"剪枝 0"（出现剪枝 N>0 说明误开了 `--prune`，⚠️ 可能漏数据，见踩坑#29） |
| 只读监听 `watch_messages.py`（v2.6） | 合成库回归：首次 `--since` 收到历史行；第二轮只收到新增行；新增 `message_N.db` 后能补投新行；水位 JSON 原子写入且重复启动不重放已确认行 |

## 产出与收尾

- 交付物：Markdown（按天分组、时间+昵称+内容、系统消息标注）。
- 任务结束向用户报告：**有效消息数 / 系统消息数**（取末尾「消息口径对账」行的 X 与 Y；**别只报总数**——总数含系统消息，会与统计底座 `msg_count`、群刊展示的数字对不上）、明文数/压缩数；密钥文件和解密库位置（敏感，提示可安全删除）；微信账号无任何变更。
- 清理：确认用户不需要后，删 `all_keys.json` 与 `decrypted/`（先问，删除须用户确认）。走一键脚本时直接 `wx_export.py --purge`，并**显式告知用户缓存路径与体量**（见踩坑#17）。

## 开源前隐私自查（四类信息，缺一不可）

发布到公开仓库前，确认**四类**信息都已剥离。**第 ④ 类最容易被忽略**——写文档举例时它会悄悄溜进来。

| 类别 | 举例 | 处理 |
|---|---|---|
| ① 代码逻辑 | 脚本本身 | 可公开 |
| ② 数据 | `.wxcache/`、导出的 `.md`、密钥、解密库 | 留本地；`.gitignore` 已忽略，**别放进技能目录** |
| ③ 环境路径 | 用户名目录、微信数据目录、绿色版安装位置、具体 wxid 样例 | 换成通用占位符（如 `X:\微信数据`） |
| ④ **文档/注释里夹带的真实信息** | **真实群名**、群消息量、真实导出文件名 | **换成「示例群名」「某读书群」等占位符** |
| ⑤ 运行时默认值（`DEFAULT_CACHE` / `DEFAULT_OUTDIR`） | 作者本机的缓存/导出路径 | ✅ v1.0.0 发布版已改为通用：缓存默认 `~/.wxcache`、导出位置必须 `--outdir/--out` 显式指定。作者本机日常可用 `--cache/--outdir` 指回私有路径，与发布版互不影响 |

> ⑤ 为什么必须改：实测 `os.makedirs("Z:/xxx", exist_ok=True)` 在**盘符不存在**的机器上抛
> `FileNotFoundError: [WinError 3]`，脚本直接崩。别人机器没有你的 G 盘。
> **v1.0.0 发布版已完成**：`DEFAULT_CACHE` 改为 `~/.wxcache`（`os.path.expanduser`，跨机器恒存在），
> `DEFAULT_OUTDIR` 改为无默认值——必须显式 `--outdir/--out`。
> `DEFAULT_OUTDIR` 绝不能设 `os.getcwd()`：若在 `scripts/` 下运行，会把聊天记录写进技能目录。
> 双保险：`wx_export.py` 启动预检——cache/outdir 所在盘符不存在时**立即**给出
> `--cache/--outdir` 指引并退出，不让人白跑几分钟才崩。
> 作者/团队本机日常使用：命令带 `--cache "G:\...\.wxcache" --outdir "G:\..."` 指回私有路径，缓存复用依旧。

> **元教训**："技能与数据分离"是**要主动核验**的，不会自动发生。数据会趁你写文档、写例子、设默认值时溜进技能。
> 发布前用关键字全目录 grep 一遍：用户名 / 数据目录 / 群名 / wxid / 密钥。
> 另注：改完文档后必须重跑冒烟测试（`--list-groups`），确认纯文案改动没碰坏逻辑。
