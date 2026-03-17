# ddg-web-search

## 用途
在当前环境没有可用搜索 API，或内置 `web_search` 因缺少 API key 等原因不可用时，使用 DuckDuckGo Lite 页面配合 `web_fetch` 执行网页搜索。

本 skill 迁移自上游仓库：`openclaw/skills -> skills/jakelin/ddg-web-search`，已按当前 G3KU 本地 skill 结构接入。

## 何时使用
- 需要联网搜索，但没有可用搜索 API key
- `web_search` 失败，特别是提示缺少 Brave API key 时
- 只需要普通网页检索结果，而不是图片、视频或复杂垂类搜索

## 核心做法
让代理直接访问 DuckDuckGo Lite 搜索页：

- 基本查询：`https://lite.duckduckgo.com/lite/?q=<URL编码后的查询词>`
- 可选语言：`&kl=us-en`、`&kl=cn-zh`
- 分页：`&s=30`、`&s=60`

优先使用 `web_fetch` 抓取结果页，再从页面中提取标题、跳转链接和摘要。

## 推荐提示词模板
可直接让模型按如下方式执行：

1. 将查询词进行 URL 编码
2. 构造 DuckDuckGo Lite URL
3. 使用 `web_fetch` 获取页面
4. 提取前 5–10 条结果
5. 返回标题、链接、摘要，并给出简要结论

示例：

- 搜索：`site:docs.github.com GitHub Actions matrix strategy`
- URL：`https://lite.duckduckgo.com/lite/?q=site%3Adocs.github.com%20GitHub%20Actions%20matrix%20strategy`

## 使用说明
当你需要搜索时，可显式说明：

- “如果 `web_search` 不可用，请改用 DuckDuckGo Lite + `web_fetch` 搜索。”
- “请使用 ddg-web-search 技能搜索以下内容：<query>”

## 局限性
- DuckDuckGo Lite 不稳定支持时间过滤
- 主要是文本网页结果，不适合图片/视频搜索
- 结果来源与排序可能和其他搜索引擎不同
- 如果当前运行环境本身没有 `web_fetch` 或外网访问能力，此 skill 不能独立生效

## 兼容与适配说明
上游资源更偏向开放技能格式；当前已将其作为 **本地 skill** 接入，而不是 tool，因为它主要提供工作流与提示模式，不包含需要单独注册的可执行工具实现。

上游原始内容已保存在 `references/upstream-SKILL.md` 供对照。
