# macOS / Linux 跨平台适配可行性结论

> 对象：本仓库 `wechat-group-export`（纯 Python、Windows 微信 4.x）。
> 目的：判断朋友圈/收藏/转账/小程序/服务号/搜索索引/增量导出等上层模块，以及底层密钥提取、解密、媒体解码，能否在 macOS、Linux 上复用。
> 证据分级：
> - 【已验证】= 本仓库源码或上游 `TANGandXue/wcdb-key-tool` 源码中可逐行核对的事实；
> - 【推断】= 基于公开资料与微信 4.x 统一代码库的合理判断，未在真机验证。

---

## 1. 数据布局：三平台路径对照

| 平台 | 微信版本 | db_storage 路径 | 证据 |
|------|----------|------------------|------|
| Windows（现状） | 微信 4.x | `%APPDATA%\Tencent\xwechat\config\*.ini` 指向的数据根 → `xwechat_files\<wxid>_xxxx\db_storage`；媒体在同账号目录 `msg/attach`、`msg/file`、`msg/video` | 【已验证】本仓库 `wcdb_key_tool_windows.py:auto_detect_db_dir()`；本机实测 `D:\weixinhuancun\xwechat_files\wxid_b8uciz1dhers22_8bb3` |
| macOS | 微信 4.x（WeChat 4.x for Mac） | `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage` | 【已验证】上游 `wcdb_key_tool_macos.py:auto_detect_db_dir()`（第 533–555 行）。注：微信 3.x for Mac 的老路径是 `~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/WeChat/`，与 4.x 不同，不可混用 |
| Linux | 官方原生 Linux 微信 4.x | `~/.local/share/com.tencent.wechat/xwechat_files/<wxid>/db_storage`（另兜底 `~/.xwechat`、`~/.local/share/wechat`） | 【已验证】上游 `wcdb_key_tool.py:auto_detect_db_dir()`（第 657–696 行） |

**结论**：4.x 三平台都沿用 `xwechat_files/<wxid>/db_storage` 这一层级结构，只是根目录不同。上层脚本只要把"数据根探测"做成按 `sys.platform` 分支的配置项，其余相对路径（`db_storage`、`msg/attach` 等）可复用。

### 1.1 SQLite / WCDB 层是否一致

- 【已验证，密码学层完全一致】上游三个脚本里 `PAGE_SZ=4096`、`SALT_SZ=16`、`RESERVE_SZ=80`、`verify_enc_key()`（salt XOR 0x3A → PBKDF2-HMAC-SHA512(2 轮) → HMAC-SHA512 校验 page1）、`_derive_keys_from_passphrase()`（PBKDF2-HMAC-SHA512 **256000 轮**）、`_decrypt_page()`（page1 回写 `SQLite format 3\0` 头）逐字节相同。SQLCipher4 的文件格式是平台无关的。
- 【已验证，表结构在 Windows 本机】解密库实测含 `message_0.db`…`message_8.db`（消息分表）、`contact.db`、`media_0.db`、`media_1.db`、`session.db`、`sns.db`（朋友圈）、`favorite.db`（收藏）、`biz_message_0.db`（服务号/企业号消息）、`Name2Id` 表（见 `export_voice.py` 第 120 行 `SELECT rowid,user_name FROM Name2Id`）。
- 【推断，消息表 schema】`Msg_<会话hash>` 分表、`Name2Id`、`local_type & 255`（3=图片/34=语音/43=视频/49=文件系）这套 Windows 4.x 已确认的 schema，macOS/Linux 4.x 因共用同一套 WCDB 与统一客户端代码，**大概率一致**；但本仓库没有 mac/linux 真机库样本，落地前需用一台 mac/linux 真机解密后跑 `SELECT name FROM sqlite_master` 核对一次。这是唯一需要真机确认的点。

---

## 2. 密钥提取机制逐条判断

本仓库靠 Windows `kernel32` 的三件套：`OpenProcess` / `ReadProcessMemory` / `VirtualQueryEx`（见 `wcdb_key_tool_windows.py` 第 438–470 行、`extract_keys_413.py` 第 37–136 行、`media_common.py` 第 221–360 行）。三平台对应物如下：

| 能力 | Windows（现状） | macOS | Linux |
|------|------------------|-------|-------|
| 枚举进程可读内存区 | `VirtualQueryEx` + `MEMORY_BASIC_INFORMATION` | `mach_vm_region`（`vm_region_basic_info_64`） | 读 `/proc/<pid>/maps` |
| 读进程内存 | `ReadProcessMemory` | `mach_vm_read` + `mach_vm_deallocate` | `/proc/<pid>/mem` 或 `process_vm_readv` |
| 打开进程句柄 | `OpenProcess(PROCESS_VM_READ\|PROCESS_QUERY)` | `task_for_pid`（Mach port） | attach 需 `ptrace` / root / `CAP_SYS_PTRACE`，受 `/proc/sys/kernel/yama/ptrace_scope` 限制 |
| 找主进程 PID | `tasklist /FI IMAGENAME eq Weixin.exe` | `pgrep -x WeChat` | 遍历 `/proc/*/exe` 找 `/wechat` 结尾 |
| 4.1+ 取 passphrase | 只读运行时扫 `Config.Cipher` 对象（主路径） | LLDB 断系统公开符号 `CCKeyDerivationPBKDF`，等用户重新登录触发 | GDB 断点：ELF 静态分析定位函数 VA + `/proc/pid/maps` 算运行时地址，等用户重新登录触发 |

【已验证】macOS/Linux 这两套"取原料"的代码上游已经写好并真机验证过（`wcdb_key_tool_macos.py:capture_passphrase_lldb()`、`wcdb_key_tool.py:capture_passphrase()` + `find_hook_offset()`），本仓库跨平台时**不需要重写，直接移植**，见 `WCDB_KEY_TOOL_RESEARCH.md` 第 2.1/2.2 节。

### 2.1 必须注意的 macOS/Linux 专属前置条件（【已验证】来自上游 README 与源码）

- macOS：首次必须 `sudo codesign --force --deep --sign - /Applications/WeChat.app` 去 Hardened Runtime，否则 `task_for_pid` 被内核拒；微信每次自动更新后可能要重签一次；需要 `xcode-select --install` 提供 lldb；要 root。
- Linux：需要 `sudo apt install gdb`；root 或放开 `yama/ptrace_scope`；ELF 静态分析**只支持 x86_64**（`EM_X86_64==62` 硬校验），ARM Linux（如树莓派/ARM 服务器）不在上游支持范围。
- 两平台的新版本抓 passphrase 都要求**用户在微信里退出登录再登录一次**触发派生计算，抓一次后缓存 `~/.wcdb-key-tool/wechat-passphrase.json`，之后免登录重抓。

---

## 3. AES 后端：可移植，换系统库即可

本仓库所有 AES 都走 Windows 自带 `bcrypt.dll`（CNG）：
- `wcdb_key_tool_windows.py:aes_cbc_decrypt()`（AES-256-CBC，解密整库用）；
- `media_common.py` 的 `aes_ecb_decrypt_data()` 一带（AES-128-ECB，图片缩略图密钥用）。

【已验证】上游给出了同签名函数在另两个系统库的实现，换一个 `aes_cbc_decrypt` 即可，上层零改动：
- macOS：`wcdb_key_tool_macos.py` 用 ctypes 调 `libSystem.CCCrypt`（CommonCrypto）；
- Linux：`wcdb_key_tool.py` 用 ctypes 调 `libcrypto.so` 的 OpenSSL EVP（`EVP_aes_256_cbc` + `EVP_CIPHER_CTX_set_padding(0)`）。

另一条路：把 AES 后端抽象成"优先系统库、缺失则回退纯 Python AES"，或允许装 `cryptography` 包——但这违背本仓库"不引第三方加密依赖"的现有设计，**建议仍走系统 ctypes 路线**，与上游一致。

---

## 4. 模块 × 平台 可行性总表

图例：✅ 直接跑 ｜ 🟡 改路径/配置/小改即可 ｜ 🔴 必须重写且工作量大 ｜ ❌ 不可行或高风险

| 模块（本仓库脚本） | 依赖点 | Windows | macOS | Linux |
|------|--------|:---:|:---:|:---:|
| SQLite 解析 / Markdown 导出 `export_group_md.py` | 纯 `sqlite3` + `os` + 字符串处理 | ✅ | ✅ | ✅ |
| 时间过滤 `media_common.parse_time_range` | 纯 Python | ✅ | ✅ | ✅ |
| 媒体索引 `export_media_index.py`、文件拷贝 `export_files.py` | 路径拼接 + sqlite | ✅ | 🟡 | 🟡 |
| 聊天搜索索引（待做） | 在解密库上建 FTS（库内已有 `message_fts.db` 可参考） | ✅ | ✅ | ✅ |
| 增量导出（待做） | 记录 last id/时间戳，纯 sqlite 逻辑 | ✅ | ✅ | ✅ |
| 朋友圈/收藏/转账红包/小程序/服务号导出（待做） | 读 `sns.db`/`favorite.db`/`biz_message_0.db` 等，纯 sqlite | ✅ | 🟡* | 🟡* |
| 整库解密 `decrypt_all`/`_decrypt_database` | SQLCipher4 逐页逻辑 | ✅ | ✅ | ✅ |
| AES-256-CBC（整库） | `bcrypt.dll` | ✅ | 🟡 换 `CCCrypt` | 🟡 换 OpenSSL EVP |
| AES-128-ECB（图片缩略图密钥） | `bcrypt.dll` | ✅ | 🟡 换 `CCCrypt` | 🟡 换 OpenSSL EVP |
| 数据目录自动探测 | `%APPDATA%\Tencent\xwechat\config\*.ini` | ✅ | 🟡 换 Containers 路径 | 🟡 换 `~/.local/share/...` |
| 密钥提取（4.1+） | `OpenProcess`+`ReadProcessMemory`+`Config.Cipher` 扫描 | ✅ | 🔴 移植上游 LLDB 方案 | 🔴 移植上游 GDB+ELF 方案 |
| 老版本内存扫 raw key | 同上 Windows API | ✅ | 🔴 移植 `task_for_pid`+`mach_vm_read` | 🔴 移植 `/proc/mem` |
| 图片密钥内存提取 `media_common.py`（221–360 行） | kernel32 三件套扫 `WxAMConfig` | ✅ | 🔴 同 LLDB/GDB 路线重写 | 🔴 同 GDB 路线重写 |
| WXGF 未查看原图解码 `export_media.py` | 加载微信安装目录 `VoipEngine.dll`，ctypes 调导出函数 | ✅ | 🔴 改加载 `WeChat.app` 内对应 `.dylib`（需逆向导出符号名，【推断】存在但未核实） | ❌ 官方 Linux 微信不一定附带同款解码库（【推断】需另找开源 WXGF 实现） |
| 语音 SILK→WAV `export_voice.py` | 第三方 `pysilk`（cffi 包） | ✅ | 🟡 需 `pip install silk-python` 且 mac arm64 有 wheel | 🟡 需 `pip install silk-python`，Linux x86_64 一般可行 |
| 一键入口 `wx_export.py` 探测安装目录 | 读运行中 `Weixin.exe` 路径 + `APPDATA` ini | ✅ | 🟡 换 `/Applications/WeChat.app` 与 Containers 路径 | 🟡 换 `/opt/wechat`/`/usr/bin/wechat` 与 `~/.local/share` |
| `capture-experimental`（cdb 断点） | Windows SDK `cdb.exe` + `bcrypt.dll` | 🔵 研究性 | ❌ 平台专属，无意义 | ❌ 平台专属，无意义 |

\* 朋友圈/收藏/服务号等上层导出模块本身是纯 sqlite，但前提是①密钥先在该平台提取出来（🔴 那一步过了才行），②mac/linux 真机上表名/列名与 Windows 一致（【推断】一致，需真机核对）。

---

## 5. 分层结论（直接回答"能不能适配"）

### 5.1 能直接跑（纯 Python，跨平台零改动）

- 所有"解密完成之后"的解析层：Markdown 导出、媒体索引、文件拷贝、时间过滤、**聊天搜索索引、增量导出、朋友圈/收藏/转账/小程序/服务号等读库型模块**。
- SQLCipher4 解密主流程：`collect_db_files`、`verify_enc_key`、PBKDF2 派生、`_decrypt_page`、`decrypt_all`、结果 JSON 落盘。
- 依据：这些代码只用 `sqlite3`/`hashlib`/`hmac`/`struct`/`os`/`pathlib`，不碰任何 Windows API（【已验证】通读 `wcdb_key_tool_windows.py`、`export_group_md.py`、`media_common.py` 后确认）。

### 5.2 改路径配置即可（工作量小）

- 数据根目录探测：把 `auto_detect_db_dir()` 和 `wx_export.py` 的安装目录探测改成 `sys.platform` 三分支，mac/linux 的候选路径直接抄上游（第 1 节表格）。
- 密钥 JSON 里 `\\` vs `/`：上游 `_key_path_variants()` 已做分隔符归一，可直接搬。
- `pysilk` 语音解码：mac/linux 上补 `pip install silk-python` 即可，代码无需改。

### 5.3 必须重写且工作量大（但上游已有现成蓝本）

- 密钥提取：mac 走 `task_for_pid`+`mach_vm_*` 内存扫 / LLDB 断 `CCKeyDerivationPBKDF`；Linux 走 `/proc/*/mem` 内存扫 + ELF 静态分析 + GDB 断点。上游两个脚本就是现成答案，移植成本主要在"把密钥层整合成我们 `wx_export.py` 一键入口"。
- AES 后端：`bcrypt.dll` → mac `CCCrypt` / linux `libcrypto EVP`，签名不变。
- 图片密钥内存提取（`media_common.py`）：换成对应平台的进程读内存 API。

### 5.4 不可行 / 高风险（如实说明）

- **WXGF 未查看原图在 Linux 上不可行（【推断】）**：本仓库靠微信 Windows 自带 `VoipEngine.dll` 里的导出函数做 WXGF→JPG；官方 Linux 微信是否附带同名解码动态库未经证实。macOS 上大概率能从 `WeChat.app/Contents/Frameworks` 里找到对应 `.dylib` 并逆向导出符号（【推断】），Linux 则可能只能降级为"WXGF 文件原样导出、提示用户回 Windows 点开过的图才有 JPG"。
- **Linux ARM 不可行**：上游 ELF 分析硬校验 x86_64。
- **`capture-experimental` 跨平台无意义**：它本身就是 Windows cdb 专属、且上游注明从未真机验证。

---

## 6. 一句话总结

> 解密之后的一切（含计划中的朋友圈/收藏/转账/搜索索引/增量导出）跨平台零改动；解密逻辑跨平台零改动；**唯一的硬骨头是"从微信进程里拿密钥"这一步**，而这一步上游 TANGandXue/wcdb-key-tool 已经把 macOS（LLDB）和 Linux（GDB+ELF）两套真机验证过的代码写好了——把它俩按本仓库"一键入口 + 模块化"的方式接进来即可，WXGF 解码在 Linux 上需降级处理。

---

## 7. 来源与可追溯链接

- 上游仓库：<https://github.com/TANGandXue/wcdb-key-tool>（MIT，main 分支，最新 commit `79f1b5b` @ 2026-08-04）
  - macOS 脚本：`wcdb_key_tool_macos.py`（`capture_passphrase_lldb` 第 375 行、`auto_detect_db_dir` 第 533 行、`CCCrypt` 后端第 67–96 行）
  - Linux 脚本：`wcdb_key_tool.py`（`find_hook_offset` 第 266 行、`capture_passphrase` 第 461 行、`auto_detect_db_dir` 第 657 行、OpenSSL EVP 第 64–131 行）
  - README 兼容性表与前置条件：`README.md` 第 20–53、84–102、130–139 行
- 本仓库现状：`scripts/wcdb_key_tool_windows.py`（kernel32 三件套第 438–470 行、`Config.Cipher` 扫描第 321–424 行）、`scripts/media_common.py`（bcrypt AES 第 30–82 行、图片密钥内存扫第 221–360 行、VoipEngine.dll 定位第 362–411 行）、`scripts/export_voice.py`（pysilk 第 165–199 行）
- 上游 Credits 链条（若日后移植 mac/linux 代码需一并致谢）：kkocdko 的 Linux GDB 思路 <https://kkocdko.site/post/202510212134>、lopleec/wxchat-export <https://github.com/lopleec/wxchat-export>、ylytdeng/wechat-decrypt <https://github.com/ylytdeng/wechat-decrypt>
