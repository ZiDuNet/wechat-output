# wechat-group-export · 微信聊天记录导出（群聊 / 私聊 / 媒体 / 语音，Windows 实测 + macOS/Linux 跨平台代码）

从微信 4.x 的本地加密数据库提取密钥、解密，把指定群聊/私聊导出为 Markdown，并把图片/视频等媒体解密导出的工具链。**数据库密钥与图片密钥均从进程内存自动提取**；**零第三方加密依赖即可完成提取+解密**（AES 走系统库：Windows `bcrypt.dll` / macOS CommonCrypto / Linux OpenSSL）；解压富文本消息需 zstandard（1.8MB）。

> ⚠️ **平台支持状态（务必先读）**：本工具链**仅在 Windows 上经过真机实测**。
> **macOS / Linux 为 v2.4 代码级实现，尚未在任何 macOS / Linux 真机测试**——
> 相关脚本（`extract_keys_macos.py` / `extract_keys_linux.py` / `aes_backend.py` 的 darwin/linux 后端 /
> `media_common.py` 的 darwin/linux 分支）只做了语法与逻辑走查，未经真机验证。
> 在 macOS / Linux 上使用前，请先在一台真机验证密钥提取与解密全流程；因未真机测试导致的问题不在已实测范围内。
>
> 实测环境：**Windows + 微信 4.1.13.63，非管理员权限**（全流程真跑过）。
> 跨平台代码按 `sys.platform` 自动分支，详见文末「跨平台」与致谢。

## 特性

- **自动探测微信数据目录**——从运行中的 Weixin.exe 定位安装目录 → 读 config ini 找数据根目录，绿色版/安装版都认，无需手动填路径
- **密钥只从进程内存捞**（数学校验，不靠猜）：微信数据库密钥不存在任何本地文件，每次登录重新生成；脚本扫描运行中微信内存 + 破解 4.1.x 新版 XOR 混淆 → HMAC-SHA1 + salt 硬校验通过才采信
- **全程只读、不越权**：不改写微信任何文件，不依赖管理员权限
- **缓存复用**：密钥+解密库缓存后，同账号导出下一个群从约 6 分钟降到 0.5 秒
- **群名安全消歧**：片段命中多个群时列出候选退出，绝不瞎猜
- **可选 `--sqlite` 双出口**：同一次解析额外产出结构化 SQLite（groups / members / messages 三表），是统计与群刊（姊妹项目 wechat-group-digest）的数据底座
- **私聊导出**：`--user "备注/昵称"` 导出任意联系人的完整聊天记录（与群聊同源的解析/去重/发信人校验逻辑）
- **媒体导出（v2.0 新增）**：`--media "会话名|all"` 解密导出图片（V2 格式 AES+XOR 解密、WXGF 容器自动转码为完整原图），`--media-video` 顺带复制视频（明文 mp4）
- **语音导出（v2.1 新增，全 Python 原生）**：`export_voice.py` 从 `media_*.db` 的 `VoiceInfo` 表提取语音（SILK v3 BLOB，不落文件系统），用 `pysilk`（pip 安装的 Python 库）解码为 24kHz WAV——零 exe、零微信 DLL、零第三方服务，转文字留可插拔后端接口
- **语音可回溯到聊天时间线**：WAV 文件名自带秒级消息时间；每会话输出 `语音时间线.csv`；全局 `voice_map.json` 供 Markdown 导出 `--voice-map`，聊天记录里 `[语音] 🎤 <wav路径>` 直接对应当天该时刻的语音
- **语音消息显示时长**（`--with-zstd`）：消息正文是 zstd 压缩的 `<voicemsg>` 元数据（时长/格式/CDN 引用），解开后显示 `[语音 15s]`（毫秒精确）或 `[语音 ~4s]`（旧版字节估算），XML 不泄漏进 Markdown
- **图片密钥全自动提取**：扫微信进程内存中的登录态整数 code（常驻，无需用户操作）→ 派生 AES 密钥 → 模板验证 → 保存复用。实测 #[MOTHER] 私聊 1758 张图片 10 秒内全部解密成功
- **朋友圈导出（v2.3）**：解析 `sns/sns.db` 的 SnsTimeLine XML，导出正文/图片/视频/地点/分享链接，自动关联评论与点赞（本机实测 395 条）
- **收藏导出（v2.3）**：解析 `favorite/favorite.db`，按 11 种 type 分组渲染（文字/图片/语音/视频/链接/位置/文件/合并转发/笔记/小程序/视频号）
- **服务号文章导出（v2.3）**：独立库 `biz_message_0.db`，zstd 解压后按公众号分组导出标题/摘要/原文链接（实测 414 个号 / 20384 篇）
- **转账/红包/小程序导出（v2.3）**：扫 49 类 appmsg，按子类型分流——转账解析金额/备注/收付款状态，红包导出祝福语+发送人（金额微信本地不存），小程序提取 appid/标题
- **聊天搜索（v2.3）**：直接复用微信自带 FTS5 索引（约 120 万行），关键词/会话/时间过滤，0.2s 级；v2.4 加 `--type` 按消息类型（text/image/voice/video/link/file/system 等）结果侧过滤
- **增量导出（v2.3）**：状态文件记录上次导出位置，只追加新增消息，重复运行不重复导出
- **聊天统计台（v2.4）**：`chat_stats.py` 对指定群/私聊输出 Markdown 统计——消息总数（有效/系统两口径）、类型分布、发言排行 Top N、按小时/按日活跃分布、时间跨度
- **群素材包（v2.4）**：`digest_source.py` 一键产出知识库素材包 `messages.json`（逐条摘要，截断 200 字）+ `stats.json`（同源统计）+ `material.md`（话题概述/发言排行/关键摘录），供下游群刊/AI 提炼消费
- **跨全部会话批量导出（v2.4）**：`export_all_sessions.py` 一条命令跨全部群+私聊按时间窗（昨天/今天/近N天…）捞消息——「梳理昨天所有聊天记录给 AI 总结事项」。架构从消息出发：每个分库只开一次连接、每张 `Msg_` 表直接跑带 `create_time` 时间窗的 SQL，无命中会话自然 0 条、不逐个探测；产出按会话分组的 Markdown 汇总 + 可选 SQLite/JSON 结构化底座。本机 `--last 昨天`：1454 个真实会话、42 个有消息、3832 条约 15s
- **全天跨会话梳理包（v2.5）**：`export_day_digest.py` 把某一天全部会话（群+私聊+文件传输助手）导成一套阅读包——`_总览.md` 统计+清单、大会话逐个一文件按小时分节、小会话可 `--merge-under` 合并成册；引用（带被引用人+摘录）、转账/红包（金额+备注）、小程序、文件均细分渲染。与 v2.4 汇总版互补（那边单文件+底座喂 AI，这边多文件给人读），同窗条数一致可交叉验证。md5 映射从 contact.db 全量构建，绝不拿 session.db 的 last_timestamp 剪枝（懒落盘会滞后，见 SKILL.md 踩坑#29）
- **跨平台（v2.4 已落地为代码）**：同一套代码按 `sys.platform` 自动分支——AES 后端抽到 `scripts/aes_backend.py`（win=bcrypt / macOS=CommonCrypto CCCrypt / Linux=OpenSSL EVP），数据目录/找进程/内存读取/密钥提取全部三平台化。**Windows 行为逐字节不变、本机真跑回归；macOS/Linux 为代码级移植，未真机**。路线图见 `docs/CROSS_PLATFORM.md`、研究见 `docs/WCDB_KEY_TOOL_RESEARCH.md`。

## 环境要求

| 需要 | 说明 |
|---|---|
| Windows + 微信 4.x | 实测 **4.1.13.63**；4.1.13.x 同系列应可运行，其他版本见「支持范围」。**本机真跑回归** |
| macOS（可选，未真机） | 微信 4.x for Mac；前置 `sudo codesign --force --deep --sign - /Applications/WeChat.app` + `xcode-select --install` + root；代码级移植 |
| Linux（可选，未真机，仅 x86_64） | 官方原生微信 4.x；前置 `sudo apt install gdb libssl-dev` + root/放开 ptrace_scope；ARM 不支持 |
| Python 3.10+ | Windows / macOS / Linux |
| zstandard | `python -m pip install zstandard`。没装不报错，但压缩消息会显示为 `[压缩未解]` 占位符（实测约 80% 富文本消息被压缩，属刚需） |
| silk-python（语音，跨平台） | `python -m pip install silk-python`；mac/linux 同样用它解码语音，代码无需改 |
| frida（macOS 可选增强，非必装） | `pip install frida frida-tools`：mac 上优先用 Frida hook `CCKeyDerivationPBKDF` 抓 passphrase；不装则走默认 LLDB/cTypes 路线 |

## 快速开始

### 方式 A：交给 AI（推荐）

把本仓库当作一个技能交给 AI 助手（WorkBuddy / Claude 等支持技能机制的 AI），对 AI 说一句话，例如：

> 导出我的「XX交流群」聊天记录，做成 Markdown。

AI 会读取仓库内的 `SKILL.md` 自动完成：环境自检 → 探测微信目录 → 提密钥 → 解密 → 导出，并在读写微信进程内存前先征得你的同意。

### 方式 B：命令行

```bash
# 0. 一次性准备
python -m pip install zstandard

# 1. 导出群聊（--outdir 必填；缓存默认 ~/.wxcache）
cd scripts
python wx_export.py --group "群名或片段" --outdir "D:/微信群导出"

# 2. 导出私聊
python wx_export.py --user "联系人备注" --outdir "D:/微信群导出"

# 3. 导出媒体（图片自动解密；--media-video 顺带复制视频）
python wx_export.py --media all --outdir "D:/媒体导出"
python wx_export.py --media "联系人" --media-video --outdir "D:/媒体导出"

# 4. 导出语音为 WAV（可选：pip install silk-python；产出语音时间线.csv + voice_map.json）
python export_voice.py --dec "C:/Users/xxx/.wxcache/decrypted" --session "联系人" --out "D:/语音导出"

# 4b. 聊天记录 Markdown 嵌入语音（语音消息行自动带上 WAV 路径，可追溯聊天时间）
python export_group_md.py --dec "C:/Users/xxx/.wxcache/decrypted" --username "wxid_xxx"     --out "D:/导出/私聊.md" --voice-map "D:/语音导出/语音/voice_map.json"

# 5. 顺带产出统计底座库（wechat-group-digest 的前置数据）
python wx_export.py --group "群名" --outdir "D:/微信群导出" \
    --sqlite "D:/微信群导出/wechat_stats.db"

# 6. 辅助命令
python wx_export.py --list-groups     # 列出全部群名（确认群名用）
python wx_export.py --list-contacts   # 列出全部联系人（确认备注名用）
python wx_export.py --purge           # 删除缓存（密钥+解密库，敏感）
python wx_export.py --group "群名" --cache "D:/tools/.wxcache" --outdir "D:/导出"  # 自定义缓存
python wx_export.py --group "群名" --db-dir "D:/微信数据/.../db_storage"            # 手动指定数据目录

# 7. v2.3 新模块（都基于已解密库，直接复用 ~/.wxcache/decrypted，无需再提密钥）
python export_sns.py --dec "C:/Users/xxx/.wxcache/decrypted" --out "D:/导出/朋友圈.md"
python export_favorite.py --dec "C:/Users/xxx/.wxcache/decrypted" --out "D:/导出/收藏.md"
python export_biz.py --dec "C:/Users/xxx/.wxcache/decrypted" --out "D:/导出/公众号文章.md"
python export_transfer.py --dec "C:/Users/xxx/.wxcache/decrypted" --out "D:/导出/转账红包小程序.md"
python search_messages.py --dec "C:/Users/xxx/.wxcache/decrypted" --keyword "关键词"
python search_messages.py --dec "C:/Users/xxx/.wxcache/decrypted" --keyword "关键词" --type text   # v2.4 按类型过滤
python export_incremental.py --dec "C:/Users/xxx/.wxcache/decrypted" --session "群名" --out "D:/导出/群名.md"

# 8. v2.4 新模块：统计台 + 群素材包 + 跨会话批量导出（都基于已解密库）
python chat_stats.py --dec "C:/Users/xxx/.wxcache/decrypted" --session "群名" --last 30d         # 统计 Markdown 报告
python digest_source.py --dec "C:/Users/xxx/.wxcache/decrypted" --session "群名" --outdir "D:/素材包" --last 30d
python wx_export.py --all-sessions --last 昨天 --outdir "D:/导出"     # 跨全部会话按时间批量导出汇总（v2.4，一键入口）
python export_all_sessions.py --dec "C:/Users/xxx/.wxcache/decrypted" --last 7d --out "汇总.md" --sqlite 底座.db --json 底座.json

# 9. v2.5 全天跨会话梳理包（一天一梳：总览 + 逐会话文件按小时分节 + 小会话合集）
python wx_export.py --digest yesterday --outdir "D:/导出/昨天"                      # 一键入口
python wx_export.py --digest 2026-09-16 --outdir "D:/导出" --merge-under 100        # 指定日期；少于100条的会话并入合集
python export_day_digest.py --dec "C:/Users/xxx/.wxcache/decrypted" --date yesterday --outdir "D:/导出"
```

脚本遵循**程序与数据分离**：不写死任何机器路径。首次运行约 30s~8min（扫描内存提密钥），之后秒级。

## 目录结构

```
wechat-group-export/
├── SKILL.md                     # 技能说明书（含四层机制不变量、21+ 条踩坑、失效排查顺序）
├── scripts/
│   ├── wx_export.py             # 一键流水线（推荐入口；--group/--user/--media）
│   ├── extract_keys_413.py      # 数据库密钥提取 + XOR 混淆破解
│   ├── export_group_md.py       # 解密库 → 按群/私聊导出 Markdown（含发信人身份双重校验）
│   ├── extract_image_key.py     # 图片密钥自动提取（扫登录态 code 派生，全自动）
│   ├── export_media.py          # 媒体导出（图片 V2 解密 + WXGF 转码 + 视频复制）
│   ├── export_voice.py          # 语音导出（VoiceInfo 表提取 + pysilk 解码为 WAV）
│   ├── media_common.py          # 媒体解密共享库（AES-ECB/V2/模板扫描/WXGF，零依赖；内存扫描已三平台抽象）
│   ├── aes_backend.py           # 【跨平台】AES 后端抽象（win=bcrypt / mac=CommonCrypto / linux=OpenSSL EVP）
│   ├── extract_keys_macos.py    # 【跨平台·未真机】macOS 密钥提取（task_for_pid+mach_vm_read / LLDB，可选 Frida）
│   ├── extract_keys_linux.py    # 【跨平台·未真机】Linux 密钥提取（ELF 锚点串 + GDB，仅 x86_64）
│   ├── export_sns.py            # 朋友圈导出（v2.3）
│   ├── export_favorite.py       # 收藏导出（v2.3）
│   ├── export_biz.py            # 服务号文章（v2.3）
│   ├── export_transfer.py       # 转账/红包/小程序（v2.3）
│   ├── search_messages.py       # FTS 聊天搜索（v2.3；v2.4 加 --type 类型过滤）
│   ├── export_incremental.py    # 增量导出（v2.3）
│   ├── chat_stats.py            # 聊天统计台（v2.4：类型分布/发言排行/小时日活跃）
│   ├── digest_source.py         # 群素材包（v2.4：messages.json + stats.json + material.md）
│   ├── export_all_sessions.py   # 跨全部会话按时间批量导出（v2.4：Markdown 汇总 + SQLite/JSON 底座）
│   ├── export_day_digest.py     # 全天跨会话梳理包（v2.5：总览 + 逐会话文件按小时分节 + 引用/转账/红包细分）
│   ├── cnb_push.sh              # 推本仓到 CNB（自动注入正确 token + 绕开失效代理）
│   └── wcdb_key_tool_windows.py # 密钥校验/解密函数（源自 TANGandXue/wcdb-key-tool，MIT）
└── LICENSE
```

## 隐私红线（务必读）

- 工具**只读**微信进程内存与本地库，不修改微信任何文件、不采集他人账号
- 缓存（`~/.wxcache`）内含**数据库密钥、图片密钥与明文聊天记录**，属敏感数据：用完可 `--purge` 删除，或自行妥善保管
- 所有数据只在你的本地机器处理，不上传任何内容
- 涉及读取进程内存，请只在**你自己的账号、你本人同意**的前提下使用

## 支持范围与失效处理

微信版本迭代可能使本工具失效（密钥提取是最脆弱的一环）。若失效：

1. 脚本会给出诊断，而不是莫名失败；
2. 阅读 `SKILL.md` 的「微信机制·不变量」——加密/数据架构/消息本体/方法论四层骨架版本无关，变的只是细节；
3. 按「新版本适配判断顺序」表逐层排查（症状 → 卡点层级 → 对应踩坑条目）。

欢迎提交 issue 报告新版本表现（附微信版本号与失败日志）。

## 姊妹项目

[**wechat-group-digest**](https://cnb.cool/fzz198479/wechat-group-digest) · 群刊编辑部——消费本工具产出的 `wechat_stats.db`，把群聊做成"AI 提炼 + 统计瘦身"的单文件网页札记。本工具采矿，digest 办刊。

## License

MIT。内嵌 `scripts/wcdb_key_tool_windows.py` 源自 [TANGandXue/wcdb-key-tool](https://github.com/TANGandXue/wcdb-key-tool)（MIT），版权归原作者，详见 `LICENSE` 的 Third-party notices。
## 借鉴的技术与仓库（致谢）

本项目在开发过程中参考了以下开源项目与思路。**所有借鉴均为"看实现、融思路"，最终代码按
本仓库自身需求重写**；仅 `scripts/wcdb_key_tool_windows.py` 直接内嵌第三方源码（MIT，见 License 一节）。

| 项目 | 借鉴了什么 | 用途 | 许可/状态 |
|---|---|---|---|
| [sjzar/chatlog](https://github.com/sjzar/chatlog)（含 [imldy/chatlog](https://github.com/imldy/chatlog) 等 fork） | **语音导出的核心突破**：微信 4.x 语音数据不在文件系统，而在 `message/media_*.db` 的 `VoiceInfo` 表（`voice_data` BLOB，按 `svr_id` 关联消息 `server_id`）；`pkg/util/silk` 的 SILK→MP3 转码思路 | 语音提取与解码链路（`export_voice.py`） | 原仓库已被微信官方函件要求移除（2025-10，仓库仅剩说明）；fork 留存 |
| [sjzar/go-silk](https://github.com/sjzar/go-silk) | 完整 SILK SDK C 源码（csilk 目录，Skype 官方开源，BSD）与 `SKP_Silk_SDK_Decode` 调用签名、微信帧格式（`0x02#!SILK_V3` 头 + 2B 帧长前缀） | 理解解码协议；pysilk 之外的备选实现 | BSD（Skype Limited 版权声明见源码头） |
| [foyoux/pilk](https://github.com/foyoux/pilk)（silk-python / pysilk 同族） | SILK 编解码的 Python 库方案：`pilk.decode` 直接吃微信 SILK（含帧长前缀），编码方向见 [foyoux/weixin-wxposed-silk-voice](https://github.com/foyoux/weixin-wxposed-silk-voice) | **实际选用的解码库**（`pip install silk-python`） | GPLv3（pilk）/ 各自许可；本工具仅通过 pip 调用，不内嵌 |
| [kn007/silk-v3-decoder](https://github.com/kn007/silk-v3-decoder) | 经典 SILK v3 解码器（Skype 官方 SDK 编译），实测可解本工具导出的微信语音（165 帧/153KB PCM 全对） | 备选解码方案（独立 exe）；因用户偏好"纯 Python 原生"最终未采用 | 实测可用；引入需自行承担第三方二进制信任 |
| [WeChatDataAnalysis](https://github.com/WeChatDataAnalysis/WeChatDataAnalysis) | ① 图片密钥派生：`md5(code+wxid)[:16]` + `xor=code&0xFF`，code 从进程内存扫描；② V2 dat 结构（15B 头：magic + aes_size + xor_size + pad）；③ WXGF 用 `VoipEngine.dll` 的 `wxam_dec_wxam2pic_5` 转码；④ SILK→WAV 转码 | 图片解密（`media_common.py` / `extract_image_key.py`）、WXGF 转码 | 其语音下载依赖第三方付费服务 wxcdn.c3o.re（配额/兑换码）——**本仓库评估后未采用**，改为本地 VoiceInfo 直取 |
| [0xlane/wechat-dump-rs](https://github.com/0xlane/wechat-dump-rs) | 密钥从运行中微信进程提取、数据库自动解密的 Rust 工具；`media_*.db` 存语音的提示 | 总体思路印证 | 存续 |
| [TANGandXue/wcdb-key-tool](https://github.com/TANGandXue/wcdb-key-tool) | 数据库密钥校验/解密函数（`verify_enc_key` / 库文件 HMAC 校验等）；v2.3 进一步研究其 **macOS 版 `wcdb_key_tool_macos.py`（LLDB 断点系统符号 `CCKeyDerivationPBKDF`）与 Linux 版 `wcdb_key_tool.py`（ELF 锚点串 `com.Tencent.WCDB.Config.Cipher` + GDB）**，作为跨平台取密钥路线的依据，详见 `docs/WCDB_KEY_TOOL_RESEARCH.md` / `docs/CROSS_PLATFORM.md` | `scripts/wcdb_key_tool_windows.py`（内嵌）；跨平台移植路线结论 | MIT（已在 LICENSE 登记）；其 Credits 链 kkocdko / lopleec/wxchat-export / ylytdeng/wechat-decrypt |
| [TANGandXue/wcdb-key-tool · macOS 版](https://github.com/TANGandXue/wcdb-key-tool)（`wcdb_key_tool_macos.py`） | v2.4 **直接移植**为本仓库 `scripts/extract_keys_macos.py`：CommonCrypto `CCCrypt` AES 后端、`task_for_pid`+`mach_vm_region`+`mach_vm_read` 内存扫、LLDB 断 `CCKeyDerivationPBKDF` 抓 passphrase、mac 沙盒容器数据目录探测 | `scripts/aes_backend.py`（darwin 后端）、`scripts/extract_keys_macos.py`、`scripts/media_common.py`（darwin 内存分支） | MIT；本仓库按"一键入口"风格重排为 `extract/decrypt` CLI，逻辑逐行对应上游 |
| [TANGandXue/wcdb-key-tool · Linux 版](https://github.com/TANGandXue/wcdb-key-tool)（`wcdb_key_tool.py`） | v2.4 **直接移植**为本仓库 `scripts/extract_keys_linux.py`：OpenSSL EVP AES 后端、ELF 静态分析锚点串定位断点（仅 x86_64）、GDB 断点抓 passphrase、`/proc/*/mem` 内存读取 | `scripts/aes_backend.py`（linux 后端）、`scripts/extract_keys_linux.py`、`scripts/media_common.py`（linux 内存分支） | MIT；同上 |
| 跨平台 Credits 链（上游再上游，随移植一并致谢） | [kkocdko](https://kkocdko.site/post/202510212134) 的 Linux GDB 抓密钥思路；[lopleec/wxchat-export](https://github.com/lopleec/wxchat-export)；[ylytdeng/wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt)；Frida hook `CCKeyDerivationPBKDF` 思路参考 yichen-wechat-local-vault | 本仓库 macOS/Linux 取密钥路线与 macOS 可选 Frida 增强的方法论来源 | 各原作者许可；本仓库仅借鉴思路、按自身需求重写 |
| [mcncarl/yichen-skills · yichen-wechat-local-vault](https://github.com/mcncarl/yichen-skills/tree/main/yichen-wechat-local-vault) | v2.4 参考其 macOS 方案与产品设计：① `CCKeyDerivationPBKDF` 的 Frida hook 思路（本仓库作为 macOS 可选增强，默认仍走上游 LLDB/cTypes）；② 微信 4.x `local_type` 低位类型表（1文本/3图/34语音/43视频/49链接文件/10000系统）印证本仓库 `&255` 规则；③ 本地 vault/知识库组织思路——`chat_stats.py`（类型分布+发言排行+活跃时段）、`digest_source.py`（群素材包 sources/{messages.json,stats.json,material.md}）、search `--type` 过滤；④ unix 密钥/明文库 `chmod 600/700` 隐私加固 | `scripts/search_messages.py`（--type）、`scripts/chat_stats.py`、`scripts/digest_source.py`、`scripts/extract_keys_macos.py`（可选 Frida）、`scripts/aes_backend.py` | 仅借鉴思路与类型表，未照抄其 Frida 注入代码（与本仓库"默认纯 cTypes、不强制第三方加密依赖"设计一致） |
| [wxcdn.c3o.re](https://wxcdn.c3o.re)（第三方 CDN Worker） | 语音/媒体代下载服务契约（token/配额/redeem/download 端点），见 WeChatDataAnalysis `cdn_image_service.py` | **评估后未采用**：需上传微信 `global_config` 鉴权、配额付费、稳定性/隐私不可控 | 第三方服务，非开源 |
| wechat-cli / wechat-smart-organizer（本机 skill） | 命令式导出思路 | **评估后未采用**：PyPI/GitHub 无对应包，命令全为空中楼阁（SKILL.md 踩坑#1/#16） | — |

### 关键借鉴路径（源码位置，便于追溯）

本仓库开发期将参考仓库克隆在 `wechat-cli-src/` 下对照研读（不入库，仅开发期参考）：

```
wechat-cli-src/
├── WeChatDataAnalysis/src/wechat_decrypt_tool/
│   ├── image_key_resolver.py      # derive_image_keys / scan_v2_templates（图片密钥派生）
│   ├── image_key_memory_scan.py   # 扫内存找 code
│   ├── media_helpers.py           # _decrypt_wechat_dat_v4 / _wxgf_to_image / _convert_silk_to_wav
│   └── cdn_image_service.py       # wxcdn 契约（评估后未采用）
├── chatlog-fork/internal/wechatdb/datasource/v4/datasource.go
│                                   # GetVoice：SELECT voice_data FROM VoiceInfo WHERE svr_id=?
├── go-silk/csilk/                  # 完整 SILK SDK C 源码 + Decoder_Api.c（调用签名）
└── silk2mp3/                       # kn007 预编译（备选验证，最终未采用）
```

> **方法论**：微信生态的开源导出工具多次被厂商以 DMCA/函件要求下架（chatlog、WeFlow 均已移除代码），
> 因此本项目坚持"看实现、融思路、不复制代码"，核心逻辑全部自研，且**不依赖任何第三方付费服务**。
