# 审核笔记 · 2026-09-17

通读 SKILL.md / README.md / scripts/ 后的文档与代码一致性核对（版本元数据 1.0.3），发现以下缺口：

## 已知线索的核对结果

- scripts/ 实测 **16 个 .py**（含朋友圈 export_sns、收藏 export_favorite、公众号 export_biz、全文搜索 search_messages、文件索引 export_files，以及新增的增量 export_incremental、转账 export_transfer）。
- 但 README「目录结构」节仍只列 8 个脚本、SKILL.md「工具清单」仍写"五个脚本"——**文档显著落后于代码**，这批新模块的用法与目录说明均待补。

## 其他不一致

1. SKILL.md「工具清单」表格仍写"本技能 scripts/ 五个脚本"并只列 5 个，与实际 16 个 .py 严重过时（后文正文虽补了 wx_export/export_voice/export_media_index 等用法，但朋友圈/收藏/公众号/全文搜索/增量/转账均未在该表体现）。
2. README 特性节版本号止于 v2.1，未提 SKILL.md 已写到的 v2.2 媒体索引（export_media_index.py）与 export_files.py；README 命令行示例也缺这些新脚本的用法。
3. SKILL.md「踩坑实录」编号乱序：1~21 后接 23~28，第 22 条却排在 28 之后。

未改任何代码。
