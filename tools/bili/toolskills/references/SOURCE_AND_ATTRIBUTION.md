# 来源与致谢

本工具（`tools/bili`，Negi 原生）是 B 站能力包的 **Negi 原生移植**，能力思路与工具语义参考自：

- **dsh-bili-scroll**（https://github.com/c1overzz/dsh-bili-scroll ，v2.3.1，MIT License）——「每天替你刷一轮 B站」的 DSH 插件。本工具完全脱离 DSH：不依赖 dsh 内核、cordis、bundle patch、DSH profile 与 @deepseek-ai/* 包，不携带其运行时代码。
- **vendor/bilibili-mcp**（内嵌于 dsh-bili-scroll，https://github.com/adoresever/bilibili-mcp ，MIT，Copyright (c) 2026 AGI_Ananas）——搜索 / 字幕 / 评论 / 热榜 / 排行 / 收藏 / 私信等工具语义与 passport 扫码登录思路参考。
- **bilibili-api-python**（MIT）——底层 B 站 API 客户端依赖。

完整上游许可证文本见同目录 `LICENSE.dsh-bili-scroll`（MIT，版权行见文件内）与 `NOTICE.dsh-bili-scroll.md`（第三方致谢），二者均为从上游仓库镜像的原样文件。

本工具自身随 Negi 项目许可；保留上游许可文本与版权行是 MIT 许可证要求的致谢动作。

## 更新检查

- **上游仓库**：https://github.com/c1overzz/dsh-bili-scroll
- **当前镜像**：v2.3.1（main HEAD `2c7b3f14f525`，2026-10-28）
- **检查命令**：`gh api repos/c1overzz/dsh-bili-scroll/releases/latest --jq .tag_name`（或访问其 Releases 页比对）
- **语义**：上游是 DSH 插件，本工具是 Negi 原生移植。上游版本更新只用于**能力思路/工具语义对照**，不自动套用；若上游出现新的工具语义或修复，应人工评审后合入 `main/tool.py`，并在本文件与 `resource.yaml#source.commit` 更新镜像 commit。
