# bili — B站能力包（Negi 原生工具）

`bili` 是 Negi 仓库内的原生可执行资源（`tools/bili/`），复用了
`bilibili-api-python` 的能力思路实现 B 站数据能力，**与 DSH 完全无关**
（不依赖 dsh 内核 / cordis / bundle patch / DSH profile / @deepseek-ai/* 包）。

## 何时使用

- 用户要「搜 B站 视频 / 看B站热榜 / 排行榜 / 视频详情 / UP主信息与投稿」——这些**无需登录**。
- 用户要「读视频字幕原文 / 评论 / 弹幕 / 收藏夹 / 稍后再看」——需要先扫码登录。
- 用户要「每天刷一轮 B站 / 出个B站日报」——用 `daily_report`。
- 用户要「看我有哪些稿件 / 稿件审核状态」——用 `my_videos`（需登录，只读）。
- 用户要「把这个视频发到 B 站 / 投稿」——用 `publish_video`（真实改动线上内容；先 `dry_run=true`
  跑一遍上传链路，再显式去掉 `dry_run` 才提交稿件）。
- 用户要「删掉我那条稿件」——用 `delete_video`（不可逆；不带 `confirm` 时只返回预览）。
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

## 关注 / 粉丝 / 点赞（following / followers / like）

- 需要登录（凭证有效）。
- `following` / `followers`：参数 `uid`（默认当前登录 uid）、`pn`（页码）、`ps`（每页 1-50，默认 50）；返回
  `{code, message, total, list:[{mid, uname, sign, ...}]}`，code=0 表示成功。接口是
  `/x/relation/followings|followers?vmid=<uid>`，cookie 用 `SESSDATA/bili_jct/buvid3/DedeUserID`
  （**SESSDATA 必须大写**，带 Referer `space.bilibili.com/<uid>`）。
- `like`：参数 `bvid`（或 `aid`）必填、`like`（true=点赞 / false=取消，幂等，默认 true）；走
  `POST /x/web-interface/archive/like`，需要 `bili_jct` 作为 csrf；返回操作前后点赞状态
  （`before/after{count, liked}`），点赞后必须可取消恢复，取消失败要如实报。
- 注意：点赞是真实改变账号状态的操作（幂等可撤销），不要批量轰炸；单视频单次。

## 我的稿件（my_videos / publish_video / delete_video）

三条动作都作用于**当前登录账号自己的稿件**，都需要登录（凭证里要有 `SESSDATA` 与 `bili_jct`，
缺 `bili_jct` 时投稿/删稿直接拒绝，因为那两个接口吃 csrf）。

### 回读稿件列表（action="my_videos"，只读）

- 数据源是创作中心稿件列表接口，参数：`pn`（页码）、`ps`（每页条数，1-50，默认 20）、`status`
  （状态过滤，逗号分隔，默认 `is_pubing,pubed,not_pubed`）。
- **返回**：`items[]{aid, bvid, title, state, state_desc, pubtime, ctime, duration}`、`total`、
  `count`、`query`、`url`、`code`。`code=0` 才算成功，失败时把接口 `message` 与 `url` 一并带回。
- 这是投稿与删稿之后**唯一的核验口**：`submitted=True` 只代表提交成功，过审/上线要看
  `state` / `state_desc`；删除是否生效看该稿件是否不再出现在列表里。

### 投稿上传（action="publish_video"）

- **参数**：`video_path`（必填，绝对路径、真实存在、非 0 字节，后缀限
  `.mp4 .flv .avi .wmv .mov .webm .mkv`）、`title`（必填，最多 80 字）、`desc`（最多 2000 字）、
  `tags`（数组或逗号/分号/空格分隔串，去重后取前 10 个，至少要剩 1 个）、`tid`（分区 id，默认 21＝日常）、
  `copyright`（1＝自制默认 / 2＝转载）、`cover_path`（可选，`.png .jpg .jpeg .webp`）、
  `dry_run`（可选）。
- **链路**：预上传 → 取 `upload_id` → 分片 PUT → 合片 →（可选封面上传）→ 提交稿件。
  每步在 `steps[]` 里留 `url` / `http_status` / `response`，失败返回点名停在哪一步
  （`step`），并把已完成的分片信息（`upload.upload_id` / `filename` / `cid`）带回，便于复查。
- **`dry_run=true` 只跑到合片**：不提交稿件，线上不产生新稿件，返回
  `submitted=False` + 说明。这是先验证上传链路的档位，去掉 `dry_run` 才会真的投稿。
- **闸门是参数级的**：投稿不读 `config` 的 `send_enabled` 与单日配额（那套只覆盖
  `reply` / `send_message`）。它是真实改动账号线上内容的动作，未得到用户明确要求不要提交。
- 成功返回 `aid` / `bvid` / `title` / `tid` / `tags` / `cover_url`，并明确写出
  `submitted=True 只代表稿件提交成功`。

### 删除稿件（action="delete_video"，不可逆）

- **参数**：`aid` 或 `bvid` 至少给一个；`confirm` 必须显式 `true` 才真删。
  只传 `bvid` 时先走公开 view 接口换 `aid`，并核对稿件 owner 是否就是当前账号——
  不是本账号的稿件直接拒绝，不发起删除。
- **预览档位**：`confirm` 缺省或为 false → `ok=True` + `will_delete=True` +
  `confirm_required=True` + `target`（已解析出的 `aid/bvid/title/state`），不调用删稿接口。
- **真删**：按创作中心前端的真实字段提交（`aid` + `csrf` + 可选 `geetest_*`），表单体与 JSON 体
  两种形态依次尝试，`attempts[]` 带回每次的 `body_kind` / `request_body` / `http_status` /
  `response`。`code=0` 才 `deleted=True`。
- **风控形态（重要）**：删稿命中 B 站 GAIA 风控时，删除接口回 `code=340022`「验证码错误」，
  此时结果里追加 `risk_precheck`（`/x/risk/archive/del` 的 `http_status` / `response` /
  `x_bili_gaia_vvoucher` / `x_bili_gaia_param`）。回 `code=340052`「账号存在风险请先验证」时，
  该挑战只能在创作中心网页端人工过滑块，通过后把 `geetest_challenge` / `geetest_validate` /
  `geetest_seccode` 原样透传再试。**不存在可绕过的免验证码删稿接口**，也不要把它降级成重试循环。
- 删除是否生效用 `my_videos` 回读确认，工具不代替用户判断"该不该删"。

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
| `缺少 bili_jct（csrf）` | 投稿/删稿/封面都要求 csrf，凭证里没这个字段就重新 `login`，不要改用免 csrf 的接口试探 |
| 删稿回 `code=340022`，且 `risk_precheck.response.code=340052` | 账号命中 GAIA 风控：需要人在创作中心网页端过一次滑块，再把 `geetest_challenge`/`geetest_validate`/`geetest_seccode` 透传重试。HTTP 侧无法自行通过，重复提交同一请求不会让它自己解除 |
| `该 bvid 不属于当前账号` | 删除目标不是本账号稿件，工具已拒绝。核对 bvid 来源，不要换 aid 重试 |
| `publish_video` 停在某个 `step`（`preupload` / `init_upload_id` / `chunk_put_N` / `complete_upload` / `cover_up` / `submit_archive`） | 按返回里的 `step` + `error` + `steps[]` 定位那一跳：文件类前置错误（后缀、0 字节、路径不是绝对）在本地就能修；`submit_archive` 的 `code`/`message` 是平台判定，按原样回报，不要把失败说成已投稿 |

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

{"action": "my_videos", "ps": 20}

{"action": "my_videos", "status": "is_pubing"}

{"action": "publish_video", "video_path": "C:\\videos\\demo.mp4", "title": "标题", "desc": "简介", "tags": ["AI Agent", "RAG"], "tid": 231, "dry_run": true}

{"action": "publish_video", "video_path": "C:\\videos\\demo.mp4", "title": "标题", "tags": "AI Agent, RAG", "copyright": 1}

{"action": "delete_video", "bvid": "BV1x4nd6RE6o"}

{"action": "delete_video", "bvid": "BV1x4nd6RE6o", "confirm": true}
```