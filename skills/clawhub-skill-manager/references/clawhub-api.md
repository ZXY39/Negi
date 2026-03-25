# ClawHub API 摘要

以下内容基于本地项目 `D:/projects/clawhub-main` 的实现提取，作为本 skill 的稳定参考。

## 优先使用的 V1 接口

### 1. 搜索

- `GET https://clawhub.ai/api/v1/search?q=<query>&limit=<n>&highlightedOnly=1&nonSuspiciousOnly=1`
- 返回：
  - `results[]`
  - 每项包含：`score`、`slug`、`displayName`、`summary`、`version`、`updatedAt`

默认建议：

- 搜索时优先带 `nonSuspiciousOnly=1`
- 只有用户明确要看 highlighted 集合时再加 `highlightedOnly=1`

### 2. 浏览列表

- `GET https://clawhub.ai/api/v1/skills?limit=<n>&sort=downloads&nonSuspiciousOnly=1`
- 适用于没有明确 query，只想看热门 / 最新 skill 的场景。

可用排序：

- `updated`
- `downloads`
- `stars`
- `installsCurrent`
- `installsAllTime`
- `trending`

### 3. Skill 详情

- `GET https://clawhub.ai/api/v1/skills/<slug>`
- 关键字段：
  - `skill.slug`
  - `skill.displayName`
  - `skill.summary`
  - `skill.tags`
  - `skill.stats`
  - `latestVersion.version`
  - `latestVersion.createdAt`
  - `owner.handle`
  - `owner.displayName`
  - `moderation.isSuspicious`
  - `moderation.isMalwareBlocked`
  - `moderation.verdict`

### 4. 指定版本详情

- `GET https://clawhub.ai/api/v1/skills/<slug>/versions/<version>`
- 可用于获取：
  - `version.version`
  - `version.createdAt`
  - `version.changelog`
  - `version.files[]`
  - `version.security`

### 5. 下载 ZIP

- `GET https://clawhub.ai/api/v1/download?slug=<slug>`
- 指定版本：`GET https://clawhub.ai/api/v1/download?slug=<slug>&version=<version>`
- 这是 skill 下载的主入口，不挂在 `/api/v1/skills/<slug>/download` 下。

已知状态码语义：

- `200`：返回 ZIP
- `403`：恶意或当前不可下载
- `423`：等待安全扫描
- `404`：skill 或 version 不存在
- `410`：版本不可用或已被移除

## ZIP 包结构

ClawHub 下载 ZIP 具有两个关键特点：

- ZIP 根目录就是 skill 内容本身，不额外包一层仓库目录
- 会额外写入 `_meta.json`

`_meta.json` 字段：

- `ownerId`
- `slug`
- `version`
- `publishedAt`

## 安全与维护建议

- 搜索默认走 `nonSuspiciousOnly=1`
- 安装前一定先查 `/api/v1/skills/<slug>` 的 `moderation`
- 对恶意阻断 (`isMalwareBlocked=true`) 直接拒绝
- 对可疑 (`isSuspicious=true`) 只有用户明确接受风险时才继续
- 版本维护优先比较本地 `current_version.version` 与远端 `latestVersion.version`

## Legacy 接口

ClawHub 仍保留旧接口：

- `/api/search`
- `/api/skill?slug=<slug>`
- `/api/skill/resolve?slug=<slug>&hash=<sha256>`
- `/api/download?slug=<slug>`

默认不要使用 legacy 路由；只有新版接口失效且明确需要兼容时才回退。
