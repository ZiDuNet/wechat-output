# wechat-group-export · 微信聊天记录导出（群聊 / 私聊 / 媒体 / 语音，Windows）

从微信 Windows 4.x 的本地加密数据库提取密钥、解密，把指定群聊/私聊导出为 Markdown，并把图片/视频等媒体解密导出的工具链。**数据库密钥与图片密钥均从进程内存自动提取**（图片密钥由登录态 code 派生，全自动、无需打开图片）；**零第三方依赖即可完成提取+解密**；解压富文本消息需 zstandard（1.8MB）。

> 实测环境：Windows + 微信 4.1.13.63，非管理员权限，自动探测数据目录（绿色版/安装版均可）。

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
- **图片密钥全自动提取**：扫微信进程内存中的登录态整数 code（常驻，无需用户操作）→ 派生 AES 密钥 → 模板验证 → 保存复用。实测 #[MOTHER] 私聊 1758 张图片 10 秒内全部解密成功

## 环境要求

| 需要 | 说明 |
|---|---|
| Windows + 微信 4.x | 实测 **4.1.13.63**；4.1.13.x 同系列应可运行，其他版本见「支持范围」 |
| Python 3.10+ | Windows 版 |
| zstandard | `python -m pip install zstandard`。没装不报错，但压缩消息会显示为 `[压缩未解]` 占位符（实测约 80% 富文本消息被压缩，属刚需） |

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
│   ├── media_common.py          # 媒体解密共享库（AES-ECB/V2/模板扫描/WXGF，零依赖）
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
| [TANGandXue/wcdb-key-tool](https://github.com/TANGandXue/wcdb-key-tool) | 数据库密钥校验/解密函数（`verify_enc_key` / 库文件 HMAC 校验等） | `scripts/wcdb_key_tool_windows.py`（内嵌） | MIT（已在 LICENSE 登记） |
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
