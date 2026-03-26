# ClawHub Skill Manager

统一处理从 `clawhub.ai` 搜索、下载、安装、更新现成 skill 的唯一工作流。

注意：ClawHub 来源的 skill 默认视为面向第三方项目、第三方运行时或第三方协作流程编写的上游 skill，不应默认认定为已经适配 G3KU 的本地 skill。

只要用户诉求属于“找 skill / 装 skill / 更新 skill / 检查 skill 是否有新版”，先走本 skill；不要绕过它手工 `curl`、手工解压，也不要直接把 `skill-creator` 当成下载器。
如果诉求涉及安装或更新，安装前必须先考虑该 skill 是否需要重写 `SKILL.md`、触发规则、资源描述、工具假设、目录路径、命令约束或输出约定，才能满足当前 G3KU 项目的要求。

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
- 默认把 ClawHub skill 视为“第三方上游素材”，而不是可直接在 G3KU 中投入使用的现成成品。
- 安装 / 更新前先评估是否需要按 G3KU 规范重写提示词、触发词、依赖说明、工具调用假设、路径与命令写法。
- 对 `isMalwareBlocked=true` 的 skill 一律拒绝继续。
- 对 `isSuspicious=true` 的 skill，只有用户明确接受风险时才允许继续。
- 安装目录固定在当前工作区的 `skills/<skill_id>/`。
- 始终使用 `scripts/clawhub_skill_manager.py`，不要手工拼 `resource.yaml`。
- 覆盖本地已有 skill 前，脚本会把旧目录备份到 `.tmp/clawhub-skill-manager/backups/`。
- 搜索 / 详情 / 状态检查默认启用同进程请求节流、429/502/503/504 有界重试、`Retry-After` 解析，以及 `.tmp/clawhub-skill-manager/cache/` 下的文件缓存，降低连续请求触发限流的概率。

## 标准工作流

1. **先判断目标是搜索、安装还是更新。**
2. **如果涉及安装或更新，先判断该 skill 是否只是第三方项目 skill，是否需要重写后才能适配 G3KU。**
3. **按需读 reference：**
   - API 与返回结构：`references/clawhub-api.md`
   - 本地包装、版本记录和备份规则：`references/local-management.md`
4. **调用脚本，不手工实现 HTTP / ZIP / manifest 逻辑。**
5. **安装或更新后确认结果：**
   - 目标目录存在于 `skills/<skill_id>/`
   - 至少包含 `SKILL.md` 和 `resource.yaml`
   - `resource.yaml` 中有 `x_g3ku.clawhub.installed_version`
6. **若评估结果显示不适配 G3KU，把本次安装视为“引入上游素材”，并继续转 `skill-creator` 做重写或结构改造。**
7. **向用户回报：**
   - 选中的 slug / skill 名称
   - 安装或更新到的版本
   - 本地目录路径
   - 是否判定仍需按 G3KU 要求重写或补齐
   - 若命中限流或上游暂时失败，说明脚本已自动节流、解析 `Retry-After` 并在边界内重试；达到上限时提示稍后重试。

## 标准命令

在工作区根目录执行：

```powershell
python skills/clawhub-skill-manager/scripts/clawhub_skill_manager.py search --query "browser"
python skills/clawhub-skill-manager/scripts/clawhub_skill_manager.py inspect --slug "browser-use-mcp"
python skills/clawhub-skill-manager/scripts/clawhub_skill_manager.py install --slug "browser-use-mcp"
python skills/clawhub-skill-manager/scripts/clawhub_skill_manager.py status --skill-id "browser-use-mcp"
python skills/clawhub-skill-manager/scripts/clawhub_skill_manager.py update --skill-id "browser-use-mcp"
```

常用参数：

- `--skills-dir <path>`：覆盖默认安装目录
- `--temp-root <path>`：覆盖 `.tmp/clawhub-skill-manager/` 根目录；其下会保存 staging、备份和 API 缓存
- `--version <semver>`：安装 / 更新指定版本
- `--force`：允许覆盖本地已有目录（会先备份）
- `--allow-suspicious`：仅在用户明确接受风险时使用
- `CLAWHUB_REQUEST_THROTTLE_SECONDS`：调整同进程请求最小间隔
- `CLAWHUB_HTTP_MAX_RETRIES` / `CLAWHUB_BACKOFF_BASE_SECONDS` / `CLAWHUB_BACKOFF_MAX_SECONDS`：调整限流与 5xx 退避参数
- `CLAWHUB_CACHE_TTL_SECONDS` / `CLAWHUB_CACHE_MAX_AGE_SECONDS`：调整 API 缓存命中和失败兜底的最大年龄

## 输出约定

脚本统一输出 JSON：

- 成功：stdout 输出 `{ "ok": true, ... }`
- 失败：stderr 输出 `{ "ok": false, "error": "..." }`

关键字段：

- `search`：`results[]` 内含 `slug`、`name`、`owner`、`latest_version`、`suspicious`、`blocked`、`url`
- `inspect`：返回版本列表与 `moderation` 摘要
- `install` / `download`：返回 `skill_id`、`installed_version`、`target_dir`、`manifest_path`、`backup_dir`
- `status`：返回 `installed_version`、`latest_version`、`up_to_date`
- `update`：返回是否实际更新、目标版本和备份目录

## 边界与禁止事项

- 不要把 ClawHub skill 直接解压到工作区根目录。
- 不要跳过安全状态检查。
- 不要因为能从 ClawHub 下载，就默认认为该 skill 已符合 G3KU 的上下文、工具、路径和治理要求。
- 不要在未经脚本管理的情况下覆盖非 ClawHub 来源的本地 skill，除非用户明确要求并允许覆盖。
- 不要把 `skill-creator` 当作 ClawHub skill 下载器。
- 不要优先使用 legacy `/api/search`、`/api/skill`、`/api/download`；默认走 `/api/v1/*`。
