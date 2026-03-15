# Toolskill 编写检查清单

在接入已有开源项目、CLI、SDK 或 vendored 仓库时，交付前对照本清单检查一次，避免 toolskill 只覆盖包装层，而遗漏上游知识层。

## 一、上游资料盘点

- [ ] 已检查上游是否存在 `skills/`。
- [ ] 已检查上游是否存在 `references/`。
- [ ] 已检查上游是否存在 `templates/`。
- [ ] 已检查上游是否存在 `docs/`、`examples/`、`README`、命令帮助或专题说明。
- [ ] 已记录哪些资料需要本地镜像，哪些资料只需要稳定跳转路径。

## 二、`resource.yaml` 完整性

- [ ] 已填写 `source`。
- [ ] 已填写 `current_version`。
- [ ] `current_version.summary` 已用自然语言说明当前版本是什么。
- [ ] `current_version.compare_rule` 已用自然语言说明更新时和什么比、怎么比。
- [ ] `current_version.source_of_truth` 已说明版本说明来自哪里。
- [ ] 已填写 `governance`，且不是依赖默认 family 映射的侥幸行为。

## 三、toolskill 双层结构

- [ ] 主 `toolskills/SKILL.md` 已说明包装层契约：参数、返回结果、失败场景、运行边界。
- [ ] 主 `toolskills/SKILL.md` 已说明上游知识入口，而不是只剩参数和示例。
- [ ] 已把关键上游场景拆到本地 `toolskills/references/`。
- [ ] 主 `toolskills/SKILL.md` 已明确“什么任务读哪份 reference”。
- [ ] 未把全部上游资料直接堆进主 `SKILL.md`。

## 四、上游 skill / reference 迁移

- [ ] 如果上游存在多个 skill，已逐个评估是否需要本地 reference 入口。
- [ ] 对重要上游 skill，已建立本地 `references/*.md` 路由文件。
- [ ] 对上游 skill 下面的 `references/` 子文档，已镜像到本地或建立稳定索引路径。
- [ ] 对未镜像的内容，已有明确排除理由，而不是无意遗漏。

## 五、版本与维护说明

- [ ] toolskill 已说明当前版本说明从哪里来，例如 `package.json.version`、release tag、commit。
- [ ] 已说明后续更新时应与哪个来源对比，例如 git tag、release 页、registry 版本。
- [ ] 已说明是否会自动下载、构建或回退到全局安装。

## 六、治理与可见性

- [ ] 已定义 `governance.family`。
- [ ] 已定义至少一个 `governance.actions`。
- [ ] 主文档解释了各 action 的行为范围。
- [ ] 工具应能生成 `tool_family`，并在 Tool 管理中可见。

## 七、最终验收

- [ ] 工具可以被资源发现并成功加载。
- [ ] toolskill 主文件与 references 目录都能被资源系统识别。
- [ ] 包装已有项目时，实际运行的是仓库固定版本，而不是不受控的全局版本。
- [ ] 删除 `tools/<tool_id>/` 后，G3KU 仍能正常启动。
