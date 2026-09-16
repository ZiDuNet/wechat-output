# wcdb-key-tool 深挖研究报告

> 研究对象：<https://github.com/TANGandXue/wcdb-key-tool>（MIT 许可证）
> 研究方式：2026-09-17 将上游仓库浅克隆至本会话工作区后逐文件通读源码（`--depth 50`，main 分支，最新 commit `79f1b5b`）。
> 本仓库现状：仅移植了上游 `wcdb_key_tool_windows.py`，落地为 `scripts/wcdb_key_tool_windows.py`。
> 本文所有【已验证】均可在下述上游文件中逐行核对；【推断】会显式标注。

---

## 1. 上游仓库全貌（【已验证】）

### 1.1 仓库结构

main 分支仅 3 个 Python 脚本 + README + LICENSE，无其它分支、无文档目录：

| 文件 | 大小 | 平台 | 我们是否已移植 |
|------|------|------|----------------|
| `wcdb_key_tool_windows.py` | 43 KB | Windows 微信 4.x | ✅ 已移植为 `scripts/wcdb_key_tool_windows.py` |
| `wcdb_key_tool_macos.py` | 31 KB | macOS 微信 4.x | ❌ 未移植 |
| `wcdb_key_tool.py` | 38 KB | Linux 微信 4.x | ❌ 未移植 |
| `README.md` / `LICENSE` | — | — | 已在 README 致谢章节登记 |

### 1.2 提交历史

```
79f1b5b  2026-08-04  feat: add Windows runtime Config.Cipher scan   ← 我们移植的就是这一版
dc71b73  2026-07-07  feat: 新增 macOS/Windows 密钥提取方案
92b179d  2026-05-12  feat: wcdb-key-tool v0.1.0 — 微信 4.1+ 数据库密钥提取
```

只有 `main` 一个分支（`remotes/origin/*` 无其它分支），无 issue/PR 信息（GitHub 网页被 robots 限制未能读取，issues 数量未确认）。

### 1.3 上游自我定位（README 原文要点）

- 覆盖 **Linux / macOS / Windows** 三平台，只做"从自己进程里取自己账号数据库密钥"这一件事，不做采集、不碰服务器通信。
- 兼容性表：老版本（4.0.x）三平台都是内存扫描明文 raw key；新版本（4.1+）原料只剩 passphrase，需调试器断点抓登录瞬间的派生调用。
- 三平台"密码学后半段"完全一致（collect_db_files → PBKDF2 派生 → HMAC 校验 → AES-256-CBC 逐页解密），差异只在"怎么把原料从进程里弄出来"。

---

## 2. 我们尚未借鉴、但值得借鉴的部分

### 2.1 【已验证】macOS 完整方案 `wcdb_key_tool_macos.py`

这是本仓库目前**完全没有**的一整块能力，上游已真机验证（README 称 LLDB 路线"18/18"）：

| 机制 | 上游函数 / 常量 | 关键实现 |
|------|-----------------|----------|
| AES-256-CBC 后端 | `aes_cbc_decrypt()` → `_libSystem.CCCrypt` | ctypes 调苹果系统库 `System`，无第三方依赖；arm64/x86_64 通用 |
| 老版本内存扫描 | `_task_for_pid()` / `_enum_readable_regions()` / `_read_memory()` | Mach 系统调用 `task_for_pid` + `mach_vm_region` + `mach_vm_read`，是 Windows `OpenProcess/VirtualQueryEx/ReadProcessMemory` 的对应物 |
| 新版本抓 passphrase | `capture_passphrase_lldb()` | LLDB 在**系统公开符号** `CCKeyDerivationPBKDF`（CommonCrypto）下条件断点（长度寄存器==32），等用户退出登录再登录触发；arm64 读 `x1/x2`，x86_64 读 `rsi/rdx` |
| PID 发现 | `_find_wechat_pid()` → `pgrep -x WeChat` | 对应 Windows 的 `tasklist` |
| 数据目录自动探测 | `auto_detect_db_dir()` | `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/db_storage` |
| 前置条件 | 文件头 Prerequisite | 必须先 `sudo codesign --force --deep --sign - /Applications/WeChat.app` 去掉 Hardened Runtime，否则 `task_for_pid` 被内核拒绝；微信每次自动更新后可能要重做 |

**值得借鉴的点**：LLDB 断"系统公开符号"这条路不随微信版本变化而失效（断的是苹果自己的库，不是微信二进制），比 Windows 的 `Config.Cipher` 运行时扫描更稳。这是跨平台研究（见 `CROSS_PLATFORM.md`）macOS 路线的直接来源。

### 2.2 【已验证】Linux 完整方案 `wcdb_key_tool.py`

同样未移植，上游称"已在真机验证"：

| 机制 | 上游函数 / 常量 | 关键实现 |
|------|-----------------|----------|
| AES-256-CBC 后端 | `_load_openssl()` / `aes_cbc_decrypt()` | ctypes 调系统 `libcrypto.so` 的 OpenSSL EVP 接口（`EVP_aes_256_cbc` + 关 padding）；找不到时回退 `/usr/lib/x86_64-linux-gnu/libcrypto.so.3` 等硬编码路径 |
| ELF 静态分析 | `_ELFSection` / `_load_elf_sections()` / `find_hook_offset()` | 手解 ELF64 节表；在 `.rodata` 找锚点串 `com.Tencent.WCDB.Config.Cipher`，沿 `48 8D 35`（lea rsi）/`48 8D 3D`（lea rdi）RIP 相对寻址反推交叉引用，向前找函数头 `55 41 57` 定位断点 VA。**只支持 x86_64**（`EM_X86_64 == 62` 硬校验，ARM Linux 不在支持范围） |
| 运行时基址 | `find_runtime_base()` | 读 `/proc/<pid>/maps`，按可读可执行映射匹配微信二进制名 |
| GDB 断点抓 passphrase | `_GDB_SCRIPT_TEMPLATE` / `capture_passphrase()` | 内嵌一段 gdb python，在 `base+offset` 下条件断点，两种取参方法（方法一：`rsi` 直接是 key 指针、`rdx==32`；方法二：`*(rsi+16)==32` 且 `*(rsi+8)` 是 key 指针） |
| PID 发现 | `_find_wechat_pid()` | 遍历 `/proc/*/exe` 软链，找 `/wechat` 结尾 |
| 权限检查 | `check_prerequisites()` | 读 `/proc/sys/kernel/yama/ptrace_scope`，非 root 时提示需 `sudo` 或临时放开 ptrace |
| 数据目录自动探测 | `auto_detect_db_dir()` | 候选 `~/.local/share/com.tencent.wechat/xwechat_files/<wxid>/db_storage`、`~/.xwechat`、`~/.local/share/wechat`，再 glob `xwechat_files/*/db_storage` |

**值得借鉴的点**：ELF 静态分析"靠锚点字符串交叉引用自动找函数"的思路，README 明确说只要微信继续用 `com.Tencent.WCDB.Config.Cipher` 这个串，微信小版本升级后无需重新逆向——这是上游宣称"自动适配新版本"的核心卖点。

### 2.3 【已验证】我们 Windows 移植版与上游的一个"差距"：缓存密钥校验闸门

上游 Linux/macOS 版 `cmd_extract()` 都是**四级递进**：

1. **已缓存 `all_keys.json` 全量 HMAC 校验通过 → 直接跳过重提取**（`wcdb_key_tool.py` 约 892–915 行；`wcdb_key_tool_macos.py` 约 704–718 行）；
2. 已保存 passphrase + PBKDF2 派生；
3. 内存扫描（老版本）；
4. 调试器断点（新版本）。

而我们移植的 `scripts/wcdb_key_tool_windows.py` 的 `cmd_extract()`（约 936–966 行）只有两级：已保存 passphrase → 内存/Config.Cipher 扫描。**上游的"第 1 级"（缓存密钥全量复验、通过就跳过）没有被移植过来**。这一级恰好是后续"增量导出"模块可以直接借用的成熟模式：密钥没变就不重新跑扫描，省掉每次启动的几十秒 PBKDF2。

### 2.4 【已验证】其它已被上游验证、我们已具备或不需要的

- `~/.wcdb-key-tool/wechat-passphrase.json`（权限 0600）三平台共用同一缓存路径——我们 `PASSPHRASE_FILE` 已经就是它。
- `set-passphrase` 手动灌入外部手段（x64dbg/Frida）抓到的 passphrase——我们已移植。
- Windows 的 `capture-experimental`（cdb 断 `bcrypt!BCryptDeriveKeyPBKDF2`）我们已移植，上游注释自己也写明**从未真机验证过**，只能当研究性备用，不值得再投入。

---

## 3. README 与源码的一处不一致（【已验证】）

README 第 45 行说 Linux 用 `elf_analyzer.py` + `gdb_capture.py` 两个文件，但当前 main 分支**实际只有 `wcdb_key_tool.py` 一个文件**，ELF 分析和 GDB 脚本都内嵌在里面（`_load_elf_sections`、`find_hook_offset`、`_GDB_SCRIPT_TEMPLATE`）。引用上游时应以源码实际结构为准，不要照抄 README 的文件名。

---

## 4. 许可证与致谢关系

- 上游许可证：**MIT**（`LICENSE`，1103 字节），与本仓库 `LICENSE` 一致，商业/开源使用无障碍。
- 本仓库 README 末尾「借鉴的技术与仓库」已登记过 wcdb-key-tool，目前致谢的是"密钥校验/解密函数（`verify_enc_key` / 库文件 HMAC 校验等）"。
- **如果后续真的移植 macOS/Linux 脚本**，建议同步把上游 README 的 Credits 链条也带过来，因为上游本身是二创：
  - [kkocdko](https://kkocdko.site/post/202510212134) —— Linux GDB 断点法原始思路；
  - [lopleec/wxchat-export](https://github.com/lopleec/wxchat-export) —— Linux ELF 静态分析方法；
  - [ylytdeng/wechat-decrypt](https://github.com/ylytdeng/wechat-decrypt) —— 内存扫描基础代码。
- 本次只是出研究报告、未改任何文件，因此**暂不需要改动 README 致谢章节**；等真正合入 mac/linux 脚本时再追加。

---

## 5. 结论速览

1. 上游是一个**三平台密钥提取工具**，我们只搬了 Windows 一块；macOS（LLDB 断系统符号）、Linux（ELF 静态分析 + GDB）两块都是完整、真机验证过的代码，可直接作为跨平台适配的蓝本。
2. 上游"缓存密钥全量 HMAC 复验后跳过提取"这一级我们没搬，是增量导出的现成参考。
3. 上游零三方依赖的设计（Windows CNG / macOS CommonCrypto / Linux OpenSSL 都走 ctypes）与本仓库"纯 Python、不引 exe/DLL"的硬约束完全兼容。
4. README 提到的 `elf_analyzer.py`/`gdb_capture.py` 已合并进单文件，引用时以源码为准。
5. 许可证 MIT，无法律障碍；真移植时需连带致谢 kkocdko / lopleec / ylytdeng 的上游链条。
