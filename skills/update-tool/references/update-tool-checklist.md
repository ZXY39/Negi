# Tool Update 检查清单

在升级或同步已有工具后，交付前对照本清单检查一次。

## 一、版本查询

- [ ] 已读取 `resource.yaml -> source`
- [ ] 已读取 `resource.yaml -> current_version`
- [ ] 已按 `source.url` 查询上游最新状态，而不是只看本地缓存或全局安装
- [ ] 已按 `current_version.compare_rule` 做升级判断

## 二、升级结论

- [ ] 已明确写出当前版本是什么
- [ ] 已明确写出上游最新状态是什么
- [ ] 已明确写出为什么升级或为什么不升级

## 三、实现同步

- [ ] 已同步更新 vendored 代码或包装层实现
- [ ] 已同步更新 `source.ref`
- [ ] 已同步更新 `current_version.summary`
- [ ] 已同步更新 `current_version.compare_rule`
- [ ] 已同步更新 `current_version.source_of_truth`

## 四、toolskill 同步

- [ ] 主 `toolskills/SKILL.md` 已更新
- [ ] `toolskills/references/` 已同步关键变化
- [ ] 如果上游 skill / references / templates 有变化，已镜像或更新索引
- [ ] 继续满足 `skills/add-tool/references/toolskill-checklist.md` 的要求

## 五、治理与资源校验

- [ ] `resource.yaml` 仍可解析
- [ ] 工具仍能被资源系统发现与加载
- [ ] 如果工具需要出现在 Tool 管理中，`governance` 仍能生成 `tool_family`

## 六、收尾

- [ ] 文档、代码、版本说明三者已同步
- [ ] 更新结果对后续维护者是可理解、可追溯的
