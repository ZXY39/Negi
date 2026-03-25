# 本地安装与更新规则

## 目标目录

- 所有通过本 skill 安装的 ClawHub skill 都落到：`<workspace>/skills/<skill_id>/`
- `skill_id` 默认由 slug 归一化得到

## 脚本负责的事情

`scripts/clawhub_skill_manager.py` 统一负责：

- 搜索 / 浏览候选
- 读取详情与版本
- 下载 ZIP
- 安全解压
- 生成或合并本地 `resource.yaml`
- 记录来源与版本
- 覆盖前备份旧目录

不要手工重做这些逻辑。

## 本地 manifest 约定

脚本会在 `resource.yaml` 中写入或刷新：

- `schema_version`
- `kind: skill`
- `name`
- `description`
- `trigger.keywords`
- `content.main`
- `exposure.agent`
- `exposure.main_runtime`
- `source.*`
- `current_version.*`

其中关键字段：

- `source.type = clawhub`
- `source.slug`
- `source.url`
- `source.detail_api`
- `source.download_api`
- `current_version.version`
- `current_version.published_at`
- `current_version.installed_at`

后续更新时，脚本依赖这些字段识别“这是一个由 ClawHub 管理的本地 skill”。

## 更新判断

默认以以下信息作为本地 / 远端真相来源：

- 本地：`skills/<skill_id>/resource.yaml -> current_version.version`
- 兜底：`skills/<skill_id>/_meta.json -> version`
- 远端：`GET /api/v1/skills/<slug> -> latestVersion.version`

如果本地版本与远端最新版不同，就认为可更新。

## 备份策略

当安装要覆盖已有目录，或执行更新时：

- 先把旧目录复制到 `.tmp/clawhub-skill-manager/backups/<skill_id>-<timestamp>/`
- 再写入新版本内容

因此，只有在确认无需保留旧内容时才使用 `--force`。

## 覆盖规则

- 默认不覆盖现有目录
- 如果现有目录不是 `source.type=clawhub`，应先停止并提示风险
- 只有用户明确要求覆盖时才使用 `--force`

## 资源刷新

G3KU 资源系统会在后续访问时做懒刷新。

实际含义：

- 脚本写入完成后，不需要额外手工注册
- 如果下一步就要使用新装 skill，直接重新 `load_skill_context(skill_id="<skill_id>")` 即可触发可见性刷新
