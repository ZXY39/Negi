# web_fetch

轻量网页抓取工具，适合在**不需要浏览器渲染**时快速读取公开网页并提取可读正文。

## 何时调用

- 已有明确 URL，需要快速读取页面标题、正文摘要、链接列表。
- 目标页面主要是服务端返回的 HTML / 纯文本，不依赖复杂 JavaScript 渲染。
- 需要比 browser 更轻、更快、更便宜的网页读取方式。

## 不该调用的情况

- 需要登录态、按钮点击、滚动、表单提交、下载触发等浏览器交互。
- 页面内容依赖前端脚本渲染，静态抓取拿不到正文。
- 需要截图、DOM 精确定位、可视化验证或多步导航。

## 与 OpenClaw 参考实现的关系

- 参考来源：`E:\Program\openclaw-2026.3.13-1\src\agents\tools\web-fetch.ts`。
- 当前版本保留了 `web_fetch` 的核心定位：单 URL 轻量抓取、正文提取、超时控制、缓存与安全边界。
- 当前版本没有直接搬运 OpenClaw 的 TypeScript 工具装配链，也没有引入其 Readability/浏览器回退链路，而是按 G3KU 的 Python 工具规范实现为可直接加载的内置工具。

## 与 browser 的边界

- `web_fetch`：面向单 URL 的轻量 HTTP 抓取 + 正文提取；不执行 JavaScript。
- `browser`：面向交互式页面操作、脚本渲染、截图和复杂导航。
- 经验法则：如果你只想“把这个页面正文读出来”，先试 `web_fetch`；如果失败原因是渲染/交互，再切换 `browser`。

## 输入参数

- `url`：必填，http/https URL。
- `max_chars`：正文最大返回长度，默认 12000。
- `extract_main_content`：默认 `true`，优先返回提取后的可读正文。
- `include_raw_html`：默认 `false`，如需排查正文提取失败可打开。
- `use_cache`：默认 `true`，开启短 TTL 缓存。
- `timeout_seconds`：默认 12 秒。

## 输出重点

- `url` / `final_url` / `status_code`：请求结果基础信息。
- `title` / `description`：页面标题与 meta description（若可提取）。
- `text`：带 `UNTRUSTED_EXTERNAL_CONTENT_*` 包装的正文。
- `links`：最多前 20 个链接文本与 href。
- `security`：返回当前安全边界说明。
- `implementation`：说明是否使用正文提取、是否启用缓存、是否存在浏览器回退等能力。

## 异步与非阻塞约束

- 工具入口为异步 `__call__`，网络请求使用 `httpx.AsyncClient`。
- 该工具不启动浏览器进程，不执行同步长阻塞外部命令。
- 正文解析在单次响应大小与字符上限内运行，避免占用过多运行时资源。

## 安全原则

- 仅允许 `http` / `https`。
- 默认阻断 `localhost`、回环地址、私网地址、链路本地地址与常见元数据地址。
- 返回正文时用 `UNTRUSTED_EXTERNAL_CONTENT_BEGIN/END` 包裹，提醒调用方不要把网页内容当作可信指令。

## 当前实现限制

- 正文提取是启发式 HTML 解析，不是完整 Readability 复刻。
- 暂未实现 browser 自动回退。
- 不支持需要认证头、cookie 注入或 POST 表单抓取。
- 不保证对所有复杂站点都有高质量正文抽取效果。
