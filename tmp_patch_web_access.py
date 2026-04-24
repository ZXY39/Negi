from pathlib import Path

skill = Path("skills/web-access/SKILL.md")
text = skill.read_text(encoding="utf-8")
insert = """## Google Trends 实战经验（2026-04）

- 不要优先用静态抓取 `web_fetch` 直拉 Google Trends explore 页做核心证据：高概率遇到 `429 Too Many Requests`。若任务要求可复核趋势数据，应优先走 **CDP 打开页面 -> 触发真实请求 -> 从浏览器性能日志/资源列表提取接口 URL** 的路线。
- “能在搜索框输入关键词并跳到 `q=` 页面”不等于“能拿到可用趋势数据”。真正目标是拿到 `widgetdata/multiline` 等接口响应，或稳定导出 CSV，而不是只确认标题/URL 变化。
- 推荐稳定流程：
  1. 用 CDP 打开 `https://trends.google.com/trends/explore?date=today%2012-m` 或目标 explore 页；
  2. 必要时先处理 cookie/consent 弹窗；
  3. 在页面中输入关键词并提交；
  4. 若 widget DOM 为空，先滚动到底部再回顶部，等待异步组件加载；
  5. 不强依赖复杂 `/eval` 去读取图表内部状态，优先从 `performance.getEntriesByType('resource')` 中捕捉 `/trends/api/widgetdata/multiline`、`relatedsearches`、`comparedgeo` 等真实请求 URL；
  6. 对捕捉到的 URL，再用 `curl.exe` 拉取响应并落地保存，解析 `timelineData` / 峰值时间点。
- PowerShell 下必须优先使用 `curl.exe`，不要写 `curl`。`curl` 常是 `Invoke-WebRequest` 别名，容易产生与网络问题混淆的假错误。
- 对 Trends 接口的浏览器外拉取，默认加稳态参数：建议至少使用 `curl.exe --http1.1 --retry 3 --retry-delay 1`。实测同一类 `widgetdata/multiline` URL 可能一次成功、一次 `curl exit 56`，加 `--http1.1` 与重试后可恢复。不要把单次成功误判成稳定方案。
- 允许“页面 DOM 空但性能日志已有 API 请求”这种情况存在。若 `.widget-container` 的 `textContent` 为空，不代表没有数据；先看 resource/performance 中是否已经出现 `/trends/api/...` 请求。
- Consent/同意弹窗会影响渲染与交互。遇到 widget 为空、按钮缺失时，先检查并关闭弹窗，再滚动触发加载。
- 导出/接口响应落地后要注意编码：某些通过重定向保存的 `/eval` 输出文件可能是 UTF-16 LE（带 BOM）；直接按 UTF-8 读取会误判为解析失败。
- Google Trends 接口常带 anti-XSSI 前缀：响应正文形如 `)]}',\n{json...}`。解析前需要先去掉第一行前缀，再对 JSON 体做解析。
- 优先保存原始响应文件，再做解析。页面上的 `formattedTime`、可见文本、按钮文案都可能因语言、渲染时机而不稳定；而 `timelineData[].time` 与 `value` 足够用于计算峰值周与序列。
- 批量任务务必加节流：对多关键词轮询请求时，建议在每次请求/关键词切换之间增加 1~3 秒等待，并内置失败重试，降低间歇性错误与触发风控的概率。

"""
marker = "## 站点经验"
if "## Google Trends 实战经验（2026-04）" not in text:
    text = text.replace(marker, insert + marker, 1)
    skill.write_text(text, encoding="utf-8")

ref = Path("skills/web-access/references/site-patterns")
ref.mkdir(parents=True, exist_ok=True)
google = ref / "google.com.md"
google_text = """---
domain: google.com
aliases: [Google, Google Trends, trends.google.com]
updated: 2026-04-24
---
## 平台特征
- Google Trends explore 页面高度依赖前端异步渲染；标题/URL 更新并不代表图表数据已稳定可读。
- 静态抓取 explore 或相关接口可能触发 429；浏览器上下文更容易拿到真实请求链路。
- 页面可能先出现空的 widget 容器，稍后才注入内容；consent 弹窗会影响渲染和交互。

## 有效模式
- 优先使用 CDP 打开 explore 页面，处理 consent 后，通过搜索框输入关键词。
- 如果 widget 文本为空，先滚动到底部再回顶部，然后检查 `performance.getEntriesByType('resource')`。
- 优先从资源请求中提取 `/trends/api/widgetdata/multiline` URL，而不是强行从 DOM/内部状态直接读曲线值。
- 对 multiline URL 使用 `curl.exe --http1.1 --retry 3 --retry-delay 1` 拉取响应，更稳。
- 响应正文通常带 `)]}',\n` 前缀；去掉第一行后再按 JSON 解析。
- 峰值计算可直接使用 `default.timelineData[].time` 与 `value`。

## 已知陷阱
- PowerShell 下误用 `curl`（别名 Invoke-WebRequest）会造成伪网络错误；应显式使用 `curl.exe`。
- 同一类接口可能出现 `curl exit 56` 的间歇性失败；需要重试与 HTTP/1.1 降级。
- 通过重定向保存的 /eval 输出文件可能是 UTF-16 LE（含 BOM），读取时要注意编码。
- 不要把“能搜索关键词”误判为“已拿到可复核趋势数据”；必须进一步拿到 multiline/CSV 响应。
"""
google.write_text(google_text, encoding="utf-8")
print("patched")
