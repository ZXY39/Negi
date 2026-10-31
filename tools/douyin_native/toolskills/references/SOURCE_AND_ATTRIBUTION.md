# 来源与致谢

本工具（`tools/douyin_native`，Negi 原生）是抖音能力包的 **Negi 原生实现**，能力思路与架构参考自：

- **dsh-douyin-browser-local**（https://github.com/LockyBee/dsh-douyin-agent ，v1.4.6，MIT License）——DSH 原生抖音助手（有限自主观看、共享人格与兴趣记忆、原生设置和观看日报）。本工具完全脱离 DSH：不依赖 dsh 内核、cordis、DSH profile、DSH 沙箱与 @deepseek-ai/* 包，不携带其运行时代码。
- **分镜 / 取帧思路**：参考 dsh-douyin-agent 中的 storyboard 设计（其上游复用了 SAOS-3624/dsh-video-storyboard，MIT）；本工具以 Pillow 原生实现拼图，**未 vendoring** 其源码。
- **Playwright**（驱动本机 Microsoft Edge，channel=msedge）与 Python 标准库——运行依赖。

完整上游许可证文本见同目录 `LICENSE.dsh-douyin-agent`（MIT，版权行见文件内）与 `THIRD_PARTY_NOTICES.dsh-douyin-agent.md`（第三方致谢），二者均为从上游仓库镜像的原样文件。

本工具自身随 Negi 项目许可；保留上游许可文本与版权行是 MIT 许可证要求的致谢动作。

## 更新检查

- **上游仓库**：https://github.com/LockyBee/dsh-douyin-agent
- **当前镜像**：v1.4.6（main HEAD `7989640e340f`，2026-10-30；release v1.4.6 对应 `912873e2556d`）
- **检查命令**：`gh api repos/LockyBee/dsh-douyin-agent/releases/latest --jq .tag_name`（或访问其 Releases 页比对）
- **语义**：上游是 DSH 插件，本工具是 Negi 原生实现。上游版本更新只用于**能力思路/架构对照**，不自动套用；若上游出现新的架构修复或能力，应人工评审后合入 `main/tool.py`，并在本文件与 `resource.yaml#source.commit` 更新镜像 commit。
