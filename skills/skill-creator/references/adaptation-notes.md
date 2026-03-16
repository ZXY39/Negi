# Adaptation Notes

本目录现在是 G3KU 项目里的统一 `skill-creator`，由原有本地 `skill-creator` 与 `agent-skill-creator` 两部分能力整合而成。

## 整合结果

- 保留 `skill-creator` 作为唯一入口名称，避免资源发现时出现两个功能高度重叠的 skill。
- 把原 `agent-skill-creator` 的 `references/`、`scripts/`、示例和上游入口文档并入当前目录。
- 把原本偏本地规范的 `skill-creator` 能力整理为 `references/g3ku-resource-spec.md`，作为 G3KU 资源结构参考。
- 主 `SKILL.md` 改为集成版入口：先判断目标是 G3KU skill、G3KU tool，还是需要额外导出的开放标准 skill。

## 有意保留的上游痕迹

- `references/upstream-SKILL.md` 与 `references/upstream-README.md` 保留为上游原始资料，内部仍会出现 `agent-skill-creator` 命名。
- 多个参考文档仍描述开放标准 skill 的创建与分发流程；这些内容现在作为 `skill-creator` 的“外部分发能力”保留。

## 有意清理的内容

- 删除重复的 `skills/agent-skill-creator/` 目录，避免同一能力在资源系统中以两个 skill 名重复出现。
- 不保留 `__pycache__` 与示例导出缓存目录等一次性产物。

## 脚本兼容性备注

- 并入的 `scripts/export_utils.py` 已保留 Windows 控制台编码容错与 CLI 参数转发修正，可在当前仓库环境中抽样导出示例 skill。
