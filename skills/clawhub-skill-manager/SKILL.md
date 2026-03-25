# ClawHub Skill Manager

统一处理从 `clawhub.ai` 搜索、下载、安装、更新现成 skill 的唯一工作流。

只要用户诉求属于“找 skill / 装 skill / 更新 skill / 检查 skill 是否有新版”，先走本 skill；不要绕过它手工 `curl`、手工解压，也不要直接把 `skill-creator` 当成下载器。

## 何时使用

- 用户要搜索、筛选、浏览 `clawhub.ai` 上的 skill 候选
- 用户要把 ClawHub 上的 skill 下载并安装到当前工作区的 `skills/`
- 用户要更新一个已经由 ClawHub 安装到本地的 skill
- 用户要检查某个已安装 skill 是否有新版本
- 用户给的是 `slug`、ClawHub 页面链接，或明确提到 `clawhub.ai`

## 不要在这些情况使用

- 用户要从 GitHub repo/path 导入现成 skill：转用 `skill-installer`
- 用户要创建、重写、拆分、迁移或设计一个全新的 G3KU skill：转用 `skill-creator`
- 用户只是想读取某个已安装 skill 的内容：直接 `load_skill_context(skill_id="<skill_id>")`

## 默认原则

- 搜索默认启用 `nonSuspiciousOnly=1`，优先返回非可疑候选。
- 安装 / 更新前先检查详情接口中的 `moderation`。
- 对 `isMalwareBlocked=true` 的 skill 一律拒绝继续。
- 对 `isSuspicious=true` 的 skill，只有用户明确接受风险时才允许继续。
- 安装目录固定在当前工作区的 `skills/<skill_id>/`。
- 始终使用 `scripts/clawhub_skill_manager.py`，不要手工拼 `resource.yaml`。
- 覆盖本地已有 skill 前，脚本会把旧目录备份到 `.tmp/clawhub-skill-manager/backups/`。

## 标准工作流

1. **先判断目标是搜索、安装还是更新。**
2. **按需读 reference：**
   - API 与返回结构：`references/clawhub-api.md`
   - 本地包装、版本记录和备份规则：`references/local-management.md`
3. **调用脚本，不手工实现 HTTP / ZIP / manifest 逻辑。**
4. **安装或更新后确认结果：**
   - 目标目录存在于 `skills/<skill_id>/`
   - 至少包含 `SKILL.md` 和 `resource.yaml`
   - `resource.yaml` 里写入 `source.type=clawhub` 与 `current_version.version`
5. **如果后续要立即使用新装的 skill，重新调用** `load_skill_context(skill_id="<installed_skill_id>")`。

## 标准脚本入口

遵循 CEO 前门提示词里的 Python 解释器规则：优先使用 `G3KU_PROJECT_PYTHON` 或前门提供的 Python hint；只有在缺失时才回退到裸 `python`。

- 搜索：
  - `python skills/clawhub-skill-manager/scripts/clawhub_skill_manager.py search --query "<query>" --limit 8`
- 查看详情：
  - `python skills/clawhub-skill-manager/scripts/clawhub_skill_manager.py inspect --slug "<slug>"`
- 下载 / 安装：
  - `python skills/clawhub-skill-manager/scripts/clawhub_skill_manager.py install --slug "<slug>"`
- 检查本地状态：
  - `python skills/clawhub-skill-manager/scripts/clawhub_skill_manager.py status --skill-id "<skill_id>"`
- 更新：
  - `python skills/clawhub-skill-manager/scripts/clawhub_skill_manager.py update --skill-id "<skill_id>"`

如果用户明确要覆盖已有目录，可以在安装 / 更新时追加 `--force`。

## 输出要求

- 搜索时返回候选列表、`slug`、`displayName`、`summary`、`version`。
- 安装 / 更新时返回 `skill_id`、`installed_path`、`version`、`backup_path`、`detail_url`。
- 如脚本返回 `ok=false`，先根据错误判断是 API、权限、下载、ZIP、manifest 还是本地目录冲突问题，再决定是否重试或修复本 skill。

## 禁止事项

- 不要把 ClawHub skill 直接解压到工作区根目录。
- 不要跳过安全状态检查。
- 不要在未经脚本管理的情况下覆盖非 ClawHub 来源的本地 skill，除非用户明确要求并允许覆盖。
- 不要把 `skill-creator` 当作 ClawHub skill 下载器。
- 不要优先使用 legacy `/api/search`、`/api/skill`、`/api/download`；默认走 `/api/v1/*`。
