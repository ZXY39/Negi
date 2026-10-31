# bili — B站能力包（Negi 原生工具）

`bili` 是 Negi 仓库内的原生可执行资源（`tools/bili/`），复用了
`bilibili-api-python` 的能力思路实现 B 站数据能力，**与 DSH 完全无关**
（不依赖 dsh 内核 / cordis / bundle patch / DSH profile / @deepseek-ai/* 包）。

## 何时使用

- 用户要「搜 B站 视频 / 看B站热榜 / 排行榜 / 视频详情 / UP主信息与投稿」——这些**无需登录**。
- 用户要「读视频字幕原文 / 评论 / 弹幕 / 收藏夹 / 稍后再看」——需要先扫码登录。
- 用户要「每天刷一轮 B站 / 出个B站日报」——用 `daily_report`。
- 用户要「发表评论 / 发私信 / @ 某人」——用 `reply` / `send_message`，但**默认关闭**，必须先用
  `config` 显式开启并设置单日配额。
- 先执行 `status` 看登录状态与发送开关。
- 参考实现：dsh-bili-scroll（`output/dsh-plugins/dsh-bili-scroll`）的 vendor/bilibili-mcp 与扫码登录
  逻辑；本工具是其 Negi 原生移植，不携带任何 DSH 痕迹。

## 参数

所有动作统一走一个参数 `action`（必填）+ 各自相关参数。动作清单见 `resource.yaml -> parameters -> action.enum`。
完整 JSON 调用示例见本文件末尾「调用示例」。

## 返回结构

- 全部动作返回 JSON（字符串或对象，由运行时交付）。
- 只读动作：`{"ok": true, ...}`；失败：`{"ok": false, "error": "..."}`。
- 发送动作：`{"ok": true, "sent": true, "rpid"/"receipt": ...}`；被开关拦截：
  `{"ok": false, "sent": false, "blocked_by": "quota", "error": "..."}`。
- 登录链路：`login` 返回 `qrcode_url` / `qrcode_file`；`login_check` 轮询状态 `waiting/scanned/done/expired`，
  成功后凭证原子写盘并收紧权限；**返回里只有 uid 与字段存在性，绝不回显 cookie/token**。

## 登录与凭据

1. `bili` 执行 `action=login` → 用手机 B 站扫码（二维码 PNG 落在 `<data>/.g3ku/bili/qrcode_login.png`）。
2. 执行 `action=login_check` 直到 `status=done`（扫码后建议隔几秒再轮询；超时 `expired` 就重新 `login`）。
3. 凭证落盘 `<data>/.g3ku/bili/bili_credential.json`，权限收紧（Windows icacls 去继承只留当前用户 /
   POSIX chmod 600）；日志与返回不泄露 cookie/token。
4. 下次调用直接复用；`check_credential` 可验证有效性。

## 发送类：显式开关 + 单日配额 + 回执台账

- 默认关闭。`action=config` 传 `send_enabled=true` 与 `daily_quota=N` 开启（`config` 支持单独改任一字段）。
- `reply` / `send_message` 每次执行前核验开关与当日用量；配额用尽返回 `blocked_by: "quota"`。
- 发送成功后回执写入台账 `<data>/.g3ku/bili/comments.md`（`rpid` / `msg_key`），失败如实回报，不冒充成功。
- `@` 通过 `reply` 的 `at_name` 参数在正文前缀 `@昵称 ` 实现。
- ⚠️ 发送动作会以你的账号对外发言，打开前请确认风险；不要代替用户发言。

## daily_report 工作流

- 参数：`keywords`（必填，多个用逗号/分号/换行分隔）、`days`（默认7）、`limit`（默认8，最多逐条读字幕的候选数）、
  `picks`（默认5，报告挑中条数上限）、`out_dir`（默认 `<data>/output`）。
- 流程：搜近期视频 → **逐条读字幕原文（不看简介）** → 有字幕的按播放量挑值得的 → 写 Markdown 报告
  （每条含"为什么值得"（播放量+字幕关键词命中）+ 一句话结论（字幕首句）+ BV 号）。
- 失败项如实写"无字幕/跳过"并给原因，不编造；未登录时字幕读不到，报告会明确标注"未登录"。

## 安装 / 更新

- **代码**：位于 `tools/bili/main/tool.py`，无需独立安装；更新方式是修改仓库内实现与 `resource.yaml` 参数契约。
- **Python 依赖**（运行时进程所在 .venv）：
  ```powershell
  & ".\.venv\Scripts\python.exe" -m pip install "bilibili-api-python>=17.4.1,<18" "aiohttp>=3.9.0" "qrcode>=7.4,<8" "Pillow>=9.0"
  ```
  另需 `qrcode_terminal`（bilibili_api 的 login_v2 在 import 时就 import 它）。Windows 上其 sdist 构建有
  WinError 206 打包 bug，处理方式：从官方 sdist 解包 `qrcode_terminal/` 包目录放进
  `.venv/Lib/site-packages/qrcode_terminal/`（安装步骤与 Windows 坑详见
  `tools/bili/toolskills/references/README.md`）。
- 资源装载入口是 `main/tool.py` 的 `build(runtime)`，返回 `BiliTool`（`g3ku.agent.tools.base.Tool` 子类）。

## 常见失败模式与回退

| 现象 | 原因 / 处理 |
|---|---|
| `未登录。请先执行 bili login 扫码` | 字幕/评论/收藏夹/稍后再看/发送需要登录 |
| `发送类动作默认关闭` / `blocked_by: quota` | 先 `config` 开开关、设配额；配额按自然日计 |
| `二维码已过期` | 重新 `login` |
| `凭证已失效或接口风控` | 重新 `login`；B站接口可能 412/风控，重试或稍后再试 |
| 字幕 `无字幕` | 该视频没有 AI 字幕，属正常结果，不要当成失败 |
| `接口回了成功，但没有投递回执` | 私信可能没真正送到（风控/对方设置），如实回报 |
| 网络 412/风控 | 记录原因，降低频率重试；不要用假数据填充。已知：未登录时 `user_videos`（UP主投稿列表）常被 412 风控拦（`space.bilibili.com` 侧），登录后可用；`video_info/search/hot/rank` 均可匿名访问 |

## 与其他工具的关系

- 独立的可执行工具，不依赖其他 Negi 工具。
- 日报产出 Markdown 到 `<data>/output/`，可用 `filesystem_stat` / `content_open` 复核。

## 调用示例

```json
{"action": "status"}

{"action": "search", "keyword": "AI Agent", "order": "pubdate", "num": 10}

{"action": "hot", "ps": 10}

{"action": "video_info", "bvid": "BV1x4nd6RE6o"}

{"action": "user_videos", "uid": 12345, "ps": 10}

{"action": "login"}

{"action": "login_check"}

{"action": "daily_report", "keywords": "AI Agent, 大模型, RAG", "days": 7, "limit": 6, "picks": 3}

{"action": "config", "send_enabled": true, "daily_quota": 3}

{"action": "reply", "bvid": "BV1x4nd6RE6o", "text": "这条讲得很清楚，AI Agent 的部分值得反复看。", "at_name": "你的昵称"}

{"action": "send_message", "receiver_uid": 12345, "text": "你好，我看到你关于大模型的视频，想问一下……"}
```