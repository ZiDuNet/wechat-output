# Skill Review（yao-meta-skill Review Studio 2.0，2026-09-22）

被审技能：`wechat-group-export`（本仓库，SKILL.md + scripts/，v3.0）。
审查工具：`npx skills add yaojingang/yao-meta-skill` 安装，`scripts/lint_skill.py` + `scripts/trigger_eval.py` + `scripts/context_sizer.py`。

## 结论：review（无 blocker，2 条 warn）

## Gates

| Gate | 判定 | 证据 |
|---|---|---|
| Trigger Lab | `warn` | trigger_eval：domain 语义配置下 negatives 6/6 全过（钉钉/QQ/飞书/表格/翻译均不误路由）；positives 中 8 类主触发语义命中，3 条因未命中领域词短语 + 多能力归一化稀释低于阈值 0.33。详见「稀释说明」。 |
| Context Budget | `warn` | context_sizer：SKILL.md 首载 12,030 tokens（48120 字符，939 行）；整包约 143K tokens。lint_skill 提示「SKILL.md is getting long; consider moving detail into references/」。 |
| Trust Report | `pass` | lint_skill ok；`__pycache__/ *.pyc` 已 gitignore；`.wxcache/ decrypted/ all_keys.json *.db` 全部忽略，无可公开泄露密钥/记录；`.opencode/`（工具缓存）本轮已补入 gitignore。 |
| 结构 / 输出契约 | `pass` | frontmatter 含 name+description（259→334 字符）；脚本均有 main()/argparse 与 `--help`；验证命令：加密测试库端到端全过（见下）。 |

### Trigger Lab 稀释说明（不是路由回退）

`trigger_eval.score_prompt_semantic`：`semantic_coverage = 命中概念权重 / description 命中概念权重和`。
本技能 description 本轮新增 13 个触发词后同时命中 4 个领域概念（export/decrypt/media/stats），
分母由 0.80 升到 1.00，导致只命中 1 个概念的用例 0.30/1.00 < 0.33 判负（原 0.30/0.80=0.375 通过）。
这是对「多能力说明书式 description」的评估器归一化偏差，不是真实路由变差——新增触发词
（导出图片/视频/语音、热词统计、数据库解密、HTML 聊天档案、媒体归档、多账号）全部对应 v3.0 真实能力，
利于真实嵌入匹配。**处置：保留新 description，本 Gate 记为 warn（可见、可继续）。**

## 本轮修复（审查/验证驱动）

1. `SKILL.md` description 触发词扩充 13 项（v3.0 新能力：媒体/热词/画像/HTML/归档/多账号）。
2. `anti_revoke.py`：动态化——缓存表 DDL、trigger 列清单、restore 列清单均按消息表 `PRAGMA table_info` 生成（版本自适应，避免列清单漂移导致 DELETE 全部失败）；trigger 的 `deleted_at` 此前未写入导致水位查不到缓存；两段 DDL 补语句分号（此前 `)\nCREATE` 无 `;` 分隔，任一后端都会 Parse error）。已端到端验证：install → 真 DELETE（trigger 捕获进缓存）→ watch 恢复 1 条，行数复原 1。
3. `db_health.py`：新增 `--save/--diff/--watch`（定期巡检：基线快照、增量 diff、`--interval` 循环告警、每次巡检后滚动基线），`--quick` 兼容；模块 docstring 去掉错误 `with DbHealthChecker(...)` 上下文管理器示例。
4. `hardlink.py` / `anti_revoke.py` 模块 docstring 修正同样错误的 `with XxxManager(...)` 用法示例（这些类没有 `__enter__`）。
5. `.gitignore` 补 `.opencode/`。

## 验证命令（可复现）

```bash
cd scripts
python3 -m py_compile msg_reader.py keyword_stats.py export_html.py media_archive.py \
  sender_profile.py wx_accounts.py db_health.py anti_revoke.py hardlink.py wcdb_core.py
# 端到端（sqlcipher 加密假库，sqlcipher CLI 后端）：
#   sender_profile / export_html / media_archive / keyword_stats / anti_revoke(watch)
#   / db_health(save+diff) / wx_accounts(list+isolate) 全部一次通过
# meta 审查：
python3 ~/.agents/skills/yao-meta-skill/scripts/lint_skill.py <本仓库>
python3 ~/.agents/skills/yao-meta-skill/scripts/context_sizer.py <本仓库> --json
python3 ~/.agents/skills/yao-meta-skill/scripts/trigger_eval.py \
  --description "$(<description>)" --cases <wechat> --semantic-config <wechat domain config>
```

## Warn 处置（不改，理由记录）

- **SKILL.md 长度 939 行 / 12K tokens 首载**：维持单文件自包含设计（跨工具分发一个文件即可用，项目不含 references/）。
  首载 12K tokens 在主流上下文预算内（~1/15）。若未来体积继续膨胀，再将「踩坑实录」「验证方式」拆入 `references/`。
- **Trigger 阈值的多能力稀释**：如上；换语义配置或换描述风格可消除，属评估模型口径，非产品缺陷。