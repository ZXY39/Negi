# douyin_native — 抖音能力包（Negi 原生实现）

在 Negi 里原生实现的抖音能力包，**与 DSH 完全无关**（不依赖 dsh 内核、cordis、DSH
profile、@deepseek-ai/* 包、DSH 沙箱概念）。用 Playwright 通过 CDP 驱动本机真实有窗口的
Microsoft Edge（`channel="msedge"` + 常驻 user-data 目录），保留并复用真实登录态；
**不下载 Chromium、不伪造、不绕过登录**。

## 何时使用

- 用户要「刷抖音」「看推荐流」「按主题出观看报告」「取视频信息/互动数据」「读评论」
- 用户要「看我自己发了什么视频 / 我的作品列表 / 作品数据」（`action="my_works"`，只读，见「我的作品」）
- 用户要「看谁回复了我 / 谁 @ 了我 / 互动消息 / 24 小时内的新回复」并据此**针对性回复**
  （`action="interactions"`，只读，见「互动消息与针对性回复」）
- 用户要「分镜/取帧/关键画面采样」（把同一作品多张实拍画面拼成带格编号的图）
- 用户明确允许后要「评论/回复/转发」（发送类默认全部关闭，见「发送许可」）
- 用户要「发作品 / 上传视频 / 存草稿」（`action="publish"`，**草稿箱模式优先**，见「发作品」）

## 运行依赖与安装

- 纯仓库内实现：代码在 `tools/douyin_native/main/`，无需独立安装。
- 运行时依赖（需安装；缺失时 `action="health"` 会报告）：
  - Python 3.12 + `playwright`（`pip install playwright`；驱动 Edge，不需要 `playwright install` 下浏览器）
  - `Pillow`（分镜拼图；缺失时 storyboard 降级为只返回单帧截图）
- 本机 Edge：`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`（自动探测）。
- 首次调用 `douyin_native(action="open")` 会弹出一个真实 Edge 窗口，使用独立的持久化
  用户目录（`data/douyin_native/profile/`），不影响日常 Edge。

### 更新

- 更新方式是修改仓库内实现、参数契约与本文档；无第三方本体可升级。
- 站点 DOM 变化时优先调整工具内的选择器/解析逻辑（集中在 `main/tool.py` 的
  `_feed_cards` / `_open_player` / `_read_comments` / `_detect_login` / `_act_search`）。

## 登录态：持久化与判定（重要）

抖音 `sessionid` 是**会话级 cookie**：浏览器非优雅退出时不会落盘，反复重启浏览器就会丢登录。
本工具用三件事解决：

1. **单个常驻 Edge 进程**：`main/tool.py` 用 `--remote-debugging-port` 启动一个分离的
   Edge，记录 `pid/port` 到 `data/douyin_native/browser.json`；之后的动作都通过 CDP 复用
   同一个会话，不再每次 action 重建上下文。`action="close"` 会 `context.close()` 优雅关机
   让 cookie 落盘，并（必要时）结束该进程。工具实例被资源重载替换时，实例级 `close()`
   只停掉 Playwright driver 连接并清空 `_browser` / `_context`，不关闭 Edge、不丢登录态；
   Edge 本身的关闭入口始终是 `action="close"`。
2. **cookie 导出/注入**：登录成功（`login_check` 等）后立即把 douyin 域 cookie 导出到
   `data/douyin_native/login_state.json`（含 sessionid/sid_guard/sid_tt/uid_tt/ttwid/
   passport_csrf_token 等实际存在的字段名），并执行
   `icacls <file> /inheritance:r /grant:r "<当前用户>:F"` 收紧权限（仅当前用户）。
   每次启动/复用浏览器时，若现场会话已无 `sessionid`，就用 `context.add_cookies` 注入该文件
   并校验。`action="login_export"` / `action="login_restore"` 可手动触发导出/注入。
3. **登录判定多重校验**：`logged_in` 不再只看 cookie 名称。判定按优先级取：
   - 服务端接口 `/passport/web/account/info/`（`data.error_code==0` 才算已登录；返回
     「会话过期，请重新登录」即视为掉登录）——**服务端裁决优先**；
   - 页面 UI：右上角是否仍有「登录」按钮 / 头像 / 登录弹窗（`douyin_login_new_class`）——
     UI 显示未登录时**绝不**报 `logged_in=true`；
   - cookie 名存在性仅作辅助。
   每次返回都带 `login.logged_in`、`login.cookie_valid`、`login.basis`（判定依据）、
   `login.inconsistent_signals`（cookie 在但实际已掉线的信号矛盾标记）。

已知现网事实（2026-11-01 实测）：站点会给未登录会话下发 sessionid 系列 cookie，但服务端
`account/info` 已返回“会话过期”；因此**只查 cookie 名必误判**，必须按上面的多重校验。

## 使用流程

建议顺序：

1. **`action="open"`**：启动/接管 Edge 并打开抖音首页，返回页面标题/链接/登录状态/截图。
2. **登录引导（运行时步骤）**：若 `login.logged_in=false`，工具会尝试弹出扫码框并提示
   「请在打开的 Edge 窗口里扫码/手机确认登录」。**等用户完成后再**
   `action="login_check"` 复核；复核通过会自动导出 `login_state.json`。工具不会假装已登录。
3. **只读浏览**：
   - `action="login_status"`：多重校验并输出 `basis` / `cookie_valid` / 截图
   - `action="feed"`：读取当前推荐/精选流（时长/点赞/标题/作者/日期），并把当轮条目快照
     （`slot` / `aweme_id` / `link` / `title`）写入 `data/douyin_native/feed_snapshot.json`；
     若当前页面停在某条视频页，会先回到精选页再读，避免把视频页文本误当作 feed
   - `action="search", keyword=...`：多入口尝试（`/search?type=video`、general、
     `/aisearch`、真实搜索框输入回车）；未登录时统一被**登录墙**拦截（页面正文出现
     “登录后即可搜索更多精彩视频”），会如实返回 `status=blocked` + 已尝试路径清单 + 截图，
     不伪造结果、不无限重试。**2026-11-02 适配**：搜索页现网结构是
     `li > div.search-result-card > a[href*="/video/"]`（无 `[data-aeeme-id]`/`data-e2e`），
     解析改走 `_search_cards()`（`a[href*="/video/"]` → aweme_id/link/标题/时长/播放/作者），
     卡片计数也用同一锚点；返回条目带 `aweme_id`，可直接用于后续 comments/reply。
   - `action="video", slot=N`：进入播放器取视频信息（作者/标题/时长/点赞/发布时间/链接），
     并校验打开的就是快照里第 N 条（见下节）
   - `action="storyboard", slot=N, frame_count=4`：采样 N 帧并拼分镜图
   - `action="comments", slot=N`：读取当前视频**真实评论正文**（author/text/likes/time/
     comment_id，取自 `[data-e2e="comment-item"]`；**不含私信**）
   - `action="watch_report", keyword=..., max_items=..., include_frames=true`：看一轮并产出 Markdown 报告
   - `action="my_works"`：读自己账号已发布作品（创作者中心内容管理页，只读；标题/时长/发布时间/状态/播放点赞等统计）
   - `action="interactions", hours=24, tab="comment"`：读互动消息（他人回复我的评论 / @我 / 赞），
     按时间窗与动作类型过滤，并可用 `locate_item` 定位到「原视频 aweme_id + 目标评论」
4. **发送类（默认关闭）**：见下节。
5. `action="usage"` / `action="health"` / `action="close"`。

### 发送许可（评论/回复/转发/发作品，默认全部关闭）

- 默认禁止：`data/douyin_native/send_policy.json` 中 `enabled=false`。
- 只有**用户本人**手动编辑该文件把 `enabled` 置为 `true`、并把对应动作
  （`allow_comment` / `allow_reply` / `allow_forward` / `allow_publish`）置为 `true`，发送才被允许。
- 调用时还必须传 `allow_send=true`，且**必须已登录**（login_state.json 存在且页面校验通过）。
- 每个发送动作有每日配额（`daily_limit_*`，默认 3）与间隔（`interval_seconds`，默认 60s）。
- 发送后做**回执核对**（重新读取评论面板/页面文本检索发送文本）；核对不到就如实返回
  `"receipt": "未确认送达"`，绝不冒充发送成功。
- `allow_publish` 只覆盖 `mode=publish`（真的公开发作品）；`mode=draft`（默认）**不需要**
  `allow_publish`，也永远不会点「发布」。

### 点赞 / 取消点赞（action="like"）

对当前视频点赞或取消，**幂等**（已是目标状态时不点击），不需要 `send_policy` 开关（它不是发言）。

- **参数**：`like`（true=点赞 / false=取消，默认 true）；用 `aweme_id`（推荐）或 `slot` 定位视频，
  沿用与评论/回复相同的**身份校验**（`aweme_match=true` 才执行，不匹配则 `status=mismatch` 且不点赞）。
- **按钮与回执**：点 `[data-e2e="video-player-digg"]` 后，用 `_JS_LIKE_STATE` 回读
  `data-e2e-state`（`video-player-is-digged`=已赞，注意页面用的是 `is-digged`）与点赞数
  （innerText 解析，支持「万」），前后一致才算 UI 层确认（`ui_confirmed=true`）。
- **持久化核验（关键，2026-11-02 实测）**：抖音点赞按钮是**前端乐观更新**——UI 变红 + 计数 +1
  不代表服务端真的记了这次点赞。工具在 UI 确认后**自动 reload 一次**并重新校验视频身份，
  再读状态；只有 reload 后仍保持目标状态才算 `receipt=confirmed`（`persisted=true`）。
  - `status=not_persisted`：UI 已切换但 reload 后回退 → **服务端未接受**，不能算点赞成功。
    2026-11-02 实测本账号（CDP 自动化会话）点赞后计数 +1 但刷新即回滚，判定为平台侧
    静默拒绝（反自动化），工具如实返回 `receipt=未持久化`，**不冒充成功**。
  - `status=persist_unverified`：reload 后未能重新确认视频身份，持久化未核验（既不报成功也不断言失败）。
  - 若需要"真点赞"，目前只能真人手动在窗口内点；工具不尝试绕过平台风控。
- **安全**：点赞是可撤销操作（再调 `like=false`），单视频单次，不批量轰炸；
  出现 `#uc-second-verify` / 登录失效立即中止。

### 我的作品（action="my_works"，只读）

读**本账号自己发布的作品**，数据源是创作者中心内容管理页
（`creator.douyin.com/creator-micro/content/manage`），纯只读、不点任何按钮。

- **返回**：`items[]{title, duration, publish_time, status, stats}`、`total`（页面「作品 (N)」）、
  `item_count`、`screenshot`。
- **定位锚点**：卡片用 `[class*="video-card"]` 取最外层（排除嵌套子元素），标题取
  `[class*="info-title-text"]`，时长取 `[class*="badge"]`；发布时间/状态/统计从卡片文本行解析
  （标签行 → 下一行取值）。用类名**前缀**而非哈希全名（`video-card-sfaSzz` 这类哈希会随构建变）。
- **登录判定（与 www 域不同，重要）**：创作者中心域下 `_detect_login` 的头像探测不适用
  （`avatar_present` 常为 false），`account/info` 的 `error_code=1011` 又是模糊信号，容易误判
  「已登录」为 `login_required`。本动作因此**以页面实测为准**：只要 URL 仍在 `content/manage`
  且未出现「扫码登录/请登录/登录后即可」文案，就继续解析列表；只有 URL 被重定向走、或出现
  登录文案，才返回 `status=login_required`。判定结果另放 `login_wall{blocked,url,loginText}`。
- **解析不到卡片**：返回 `status=ok` + 说明（可能确实没有作品，`total=0`；或创作者中心列表结构
  变了），带截图核对，**不臆造条目**。
- 只列已发布/审核中等当前列表里的作品；草稿箱不在这个页面（内容管理没有草稿 Tab）。

### 互动消息与针对性回复（action="interactions"，只读；回复复用 reply_to_comment）

闭环：**发评论 → 他人回复/@我 → 读互动消息 → 定位原视频与评论 → 针对性回复**。

本条动作本身**纯只读**（只点开面板、点开缩略图看视频、读评论），不发送任何内容；
真正的发送只在 `reply_to_comment` 里，受 `send_policy.json` + `allow_send` 双重闸门约束。

- **入口（实测结论，勿凭直觉改）**：互动消息图标在 `https://www.douyin.com/user/self` 的
  头部导航里（`#douyin-header` → `#douyin-header-menuCt`，图标行文本含「通知」「消息」）。
  推荐流页（`/jingxuan`、`/?recommend=1`）的 header 是精简版，**没有**通知图标 → 本动作
  先 `_goto` 到 `/user/self` 再点。点击后下拉第一行是「互动消息 全部消息」，
  **必须再点「全部消息」** 才展开 tab 行：`全部消息 | 粉丝 | @我的 | 评论 | 赞 | 推荐`。
- **参数**：
  - `hours`（默认 24，即「24 小时内」）：时间窗过滤，行内相对时间会被换算成
    `age_hours_estimate` + `time_precision`（`exact` = 「刚刚/N分钟前/N小时前」可精确到小时；
    `coarse` = 「昨天/前天/N天前/M-D/YYYY-MM-DD」只能粗估，如「昨天」按 24h 计）。
  - `tab`：`all | comment | mention | like | fan | recommend`，映射到上面的 tab 行；
    `comment`（回复/评论我）与 `mention`（@我）是闭环主要入口。
  - `max_items`：返回条数上限（1-50，默认 20），按页面顺序（新→旧）取。
  - `locate_item`：对**将要返回的某条通知**（用返回里的 `index`）做二次定位，产出
    「原视频 aweme_id + 目标评论」，见下。
  - `comment_text_match`：定位时在目标视频评论里按正文/作者子串找定位锚点（不传则只给视频与评论列表）。
- **返回结构**：`items[]{index, author, action, is_reply_to_me, is_mention, is_about_my_comment,
  content, content_kind, time_text, age_hours_estimate, time_precision, within_hours,
  source_aweme_id, author_user_ids, row_lines, rect[, comment_id, matched_comment_author]}`；
  还有 `filter{rows_seen, rows_after_tab_filter, kept, dropped_by_time, dropped_time_labels,
  coarse_time_rows}`、`panel{row_count, text_head}`、`panel_probe`、`expand`、`tab_switch`、
  `login`、`second_verify`、`locate`、`screenshot`。
- **`content_kind` 必须看**：`action_phrase` = 通知行自带的动作短语（如「赞了你的评论」），
  **不是**对方正文；`comment_body` = 已通过 `locate_item` 落到视频评论区读到的真实正文。
  通知行本身通常没有正文、也没有 `/video/` 链接，所以普通列表调用拿不到 `source_aweme_id`
  与 `comment_id`——这是站点结构决定的，不是解析失败，报告里要说清楚。
- **定位（`locate_item`）**：行内**没有** `/video/` 链接，必须点开才拿到视频身份。
  - 必须点**行右侧缩略图端**（`via=row_thumbnail_end`）：点左侧会打开 `/user/<sec_uid>`
    个人主页（还会开新标签页）。代码里用 `preferVideo=true` 的 `_JS_NOTIFY_ROW_POINT` 取点。
  - 点开后 URL 变成 `/user/self?modal_id=<aweme_id>`，代码从 `modal_id` 取 aweme_id，
    再用 `_open_video_by_id` 到 `/video/<id>` 并用页面元素（`detail-video-info` 属性 /
    `player-container` 的 `video_<id>` class）**二次校验身份**，不一致会返回
    `video.status=mismatch` 并附 `target/actual`，不静默继续。
  - 定位成功输出 `locate{resolved_aweme_id, video{status,...}, comments_read, comment_match{
    index, author, text, likes, time, comment_id}, reply_hint{action:"reply_to_comment",
    aweme_id, comment_text_match, comment_id, allow_send}, ok}`。
  - `reply_hint` 可直接原样作为下一次调用的参数，**免去手工改 `feed_snapshot.json` 的 slot**。
- **针对性回复**：`reply_to_comment` 现在支持 `aweme_id`（推荐，闭环用）与 `slot`（原有路径，未变）：
  ```json
  {"action": "interactions", "tab": "comment", "hours": 24, "locate_item": 8, "comment_text_match": "大肥鱼"}
  {"action": "reply_to_comment", "aweme_id": "7703073192813989105", "comment_text_match": "大肥鱼",
   "text": "同感，谢谢补充", "allow_send": true}
  ```
  传 `aweme_id` 时走 `_open_video_by_id`（同样做页面身份校验）；不传则仍按 `slot` 打开 feed，
  原有行为与返回值不变。
- **发送闸门（必须在报告里如实体现）**：`reply_to_comment` 的先决条件是
  `send_policy.json` 里 `enabled=true` **且** `allow_reply=true`，**再**要 `allow_send=true`，
  并且要已登录。任一不满足 → `status="blocked"` + `nothing_happened=true`，**一个字符都不会输入**。
- **安全**：`/user/self` 与视频页打开后会先查 `#uc-second-verify` 短信/账号安全二次验证遮罩，
  命中即 `status="blocked_second_verify"` + `nothing_happened=true` 立即中止，**不绕过、不重试**。

### 发作品（action="publish"，草稿箱模式优先）

用 Playwright 驱动 **创作者中心上传页**（`creator.douyin.com/creator-micro/content/upload`，
实测 HTTP 200 可达），把本地视频上传并填写标题/简介/话题。

- **参数**：`video_path`（必填，本地视频绝对路径，mp4/mov/webm/m4v/avi/mkv）、`title`、`desc`、
  `tags`（数组或逗号/顿号分隔串，写入简介编辑器转话题 chip）、`mode`（`draft` 默认 / `publish`）、
  `allow_send`（必 true）、`allow_publish`（仅 mode=publish 时需要）。
- **双闸门（发作品也遵守）**：
  - 层 1（两种 mode 都要）：`send_policy.json enabled=true` + 调用传 `allow_send=true` +
    已登录 + `daily_limit_publish`（默认 3）/ `interval_seconds`（默认 60）配额。
  - 层 2（仅 `mode=publish`）：`send_policy.json allow_publish=true` **且** 调用再显式传
    `allow_publish=true`。任何一项缺失 → `status=blocked`，什么都不发生。
  - `mode=draft`（默认）：只“上传 + 填表 + 点「暂存离开」”，**不点「发布」**，不产生公开作品。
- **真实键盘输入**：上传页是 React/自研编辑器，只认真实键盘事件；标题用
  `input[placeholder*="作品标题"]`（placeholder/文本锚点，非哈希类名），简介用
  `div.zone-container[contenteditable="true"][data-placeholder="添加作品简介"]`，话题在简介
  里敲 `#话题` + `Space` 转成真实 topic chip（`<div data-mention="#">`）。一律
  `page.keyboard.type(...)`，**不用 DOM fill()/textContent 注入**（2026-11-01 实测：注入
  会出现“看着填进去了、存草稿时内容其实是空的”）。填完**回读**字段并比对。
- **大文件等待**：上传后轮询“0% → 检测中 N% → 消失”，视频 `<video>` readyState≥2 且进度
  稳定后才填表，避免“上传还没完就存草稿”。
- **风控遮罩**：出现 `#uc-second-verify` / “短信验证 / 账号安全验证” 立即中止并如实返回
  `status=blocked_second_verify`，**不绕过、不自动重试**（需本人在窗口内完成验证）。
- **低频原则**：单次调用只传一个视频；受每日配额 + 间隔约束；不批量轰炸。
- **回执核对**：
  - `mode=draft`：点「暂存离开」后上传页出现「你还有上次未发布的视频，是否继续编辑？」；
    点「继续编辑」会重开 `/content/post/video?enter_from=draft` 并**回读**到刚填的标题/简介/
    话题 → 一致才算 `receipt=confirmed`；核对不到如实报 `未确认送达`。当前创作者中心
    「内容管理」没有草稿箱 Tab（实测 2026-11-01），所以草稿存在性以上面两种信号为准。
  - `mode=publish`：点「发布」后找成功提示（发布成功/作品已发布/审核中），再兜底查
    「内容管理」列表是否出现该标题；找不到即 `receipt=未确认送达`，不冒充成功。
- **风险说明**：`mode=publish` 是真实公开发布，默认不可用；只在该调用显式 `mode=publish`
  且 `send_policy allow_publish=true` 且传 `allow_publish=true` 时才可能发生。草稿箱模式
  仍会在账号里生成一条草稿，属于可被「放弃」清除的未发布状态。建议低频、用本人账号、
  只发自己制作的素材；不要用此能力批量抓取或规避平台风控。

### slot → aweme_id 一致性（`feed_snapshot.json`，评论/回复前必读）

抖音推荐流是**会换批的个性化流**：同一个 slot 号在两次调用之间可能落到另一条视频上。历史缺陷
就是「`feed(max_items=8)` 第 1 条是 A，随后 `comments(slot=1)` 却评到了 B」。现在按下面的规则
把「要操作的那条」和「实际打开的那条」钉死：

1. `action="feed"` / `watch_report` 读完当轮条目后，把快照写入
   `data/douyin_native/feed_snapshot.json`：
   `{written_at, source, page_url, item_count, items:[{slot, aweme_id, link, title, author, duration, likes}]}`。
2. 所有按 slot 的动作（`video` / `storyboard` / `comments` / `send_comment` /
   `reply_to_comment` / `send_forward`）**先按快照把 slot 解析成 `aweme_id`**，直接跳
   `https://www.douyin.com/video/<aweme_id>`，**不再现场重读推荐流决定 slot**。
3. 跳转后校验页面实际挂载的视频身份，依次取：`[data-e2e="detail-video-info"]` 的
   `data-e2e-aweme-id` 属性 → `[data-e2e="player-container"]` 的 `class="video_<id>"`。
   **URL 里的 `/video/<id>` 不算证明**：目标不存在时路径会原样留着，所以 url-only 只当作线索。
4. 不一致时：按 `aweme_id` 重新跳转一次；仍不一致 → 返回 `status=mismatch`，附
   `target_aweme_id` + `actual_aweme_id`，**不执行评论/回复/发送，也不返回另一条视频的数据**。
5. 评论/分镜读取**结束后再校验一次**身份：读取期间若发生自动换片，返回 `status=mismatch`
   并置 `video_swapped_during_read=true`，评论正文不外泄（`comments=[]`）。
6. 快照缺失（没先跑 feed）：会现场读一次推荐流并把该 slot 钉住，`snapshot_state="retaken_now"`，
   调用方据此知道快照是本次重取的；连条目都取不到则 `status=aweme_unavailable`，不会乱开视频。
7. 返回固定带 `target_aweme_id` / `actual_aweme_id` / `aweme_match`（+ `snapshot_state`、
   `snapshot_path`），外部可独立核对。`verify_aweme_id=false` 可关闭「报错中止」，此时仍返回
   两个 id 与 `aweme_match`，只把 `verification` 标为 `disabled`（仅调试用，评论/发送不建议关）。

### 视频详情字段来源（paused 也能取到）

`/video/<id>` 是客户端渲染页面，`DOMContentLoaded` 时可能只有一个空壳（详情面板与 `<video>`
都还没挂载）——这正是「`video_present=true` 但 author/title/duration/likes/publish_time 全空」
的成因。现在读取是**轮询 + 逐字段合并**（首屏未就绪就等，最长约 12s，单字段取第一个非空值，
播放状态与身份取最后一帧），因此 paused / 未播放也能读全：

| 字段 | 来源（实测 2026-11-01） | 回退 |
| --- | --- | --- |
| `author` | `[data-e2e="user-info"]` 首行去掉「粉丝/获赞/关注」 | `[data-e2e="video-detail"] a[href*="/user/"]` |
| `title` | `document.title` 去掉「 - 抖音」 | 详情面板 `desc` |
| `detail_text` / `desc` | `[data-e2e="detail-video-info"]` innerText（剔除「展开/举报/发布时间」与纯数字统计行） | — |
| `likes` | `[data-e2e="video-player-digg"]`（如「3.7万」） | 详情面板第一个纯数字统计行 |
| `publish_time` | `[data-e2e="detail-video-publish-time"]`（去「发布时间：」） | 详情面板正文里的 `发布时间：…` |
| `duration_seconds` | `video.duration`（元数据已加载时） | 播放器时钟文本 `00:03 / 44:57` 的总时长（paused 时仍可用） |

返回里每个字段都带 `*_source`（如 `author_source=data2e=user-info`、
`duration_source=player clock text`），另有 `detail_ready`、`poll_count`、`paused`、`ready_state`、
`aweme_identity{panel,player_class,url}`，便于判断「取到了什么、从哪取的」。

### 参数与示例

```json
{"action": "open"}
{"action": "login_check"}
{"action": "feed", "max_items": 10}
{"action": "watch_report", "keyword": "美食", "max_items": 8, "include_frames": true, "frame_count": 4}
{"action": "my_works", "max_items": 20}
{"action": "interactions", "hours": 24, "tab": "comment", "max_items": 10}
{"action": "interactions", "hours": 24, "tab": "mention", "max_items": 10}
{"action": "interactions", "tab": "comment", "hours": 24, "locate_item": 8, "comment_text_match": "大肥鱼"}
{"action": "reply_to_comment", "aweme_id": "7703073192813989105", "comment_text_match": "大肥鱼", "text": "同感，谢谢补充", "allow_send": true}
{"action": "storyboard", "slot": 1, "frame_count": 4}
{"action": "comments", "slot": 1, "max_items": 30}
{"action": "send_comment", "slot": 1, "text": "好看，感谢分享", "allow_send": true}
{"action": "reply_to_comment", "slot": 1, "comment_index": 2, "text": "同感，谢谢补充", "allow_send": true}
{"action": "reply_to_comment", "slot": 1, "comment_text_match": "胶囊", "text": "同感", "allow_send": true}
{"action": "comments", "slot": 1, "max_items": 30, "verify_aweme_id": false}
{"action": "publish", "video_path": "C:\\videos\\demo.mp4", "title": "标题", "desc": "简介", "tags": ["话题A", "话题B"], "mode": "draft", "allow_send": true}
{"action": "publish", "video_path": "C:\\videos\\demo.mp4", "title": "标题", "mode": "publish", "allow_send": true, "allow_publish": true}
```

- `action`（必填）：`open | login_status | login_check | login_export | login_restore |
  feed | search | video | storyboard | comments | watch_report | my_works | interactions |
  send_comment | reply_to_comment | send_forward | publish | usage | health | close`
- `keyword`：搜索词/报告主题；`max_items`：1-40（comments 时是评论条数上限，默认 30）；
  `frame_count`：2-16；`slot`：第几个 feed 条目进播放器（1 起）；`text`：评论/回复正文；
  `comment_index`：回复目标评论的 1-based 序号（取 comments 返回的 `index`）；
  `comment_text_match`：按正文/作者子串定位回复目标（与 `comment_index` 二选一）；
  `allow_send`：发送显式许可；`verify_aweme_id`：默认 true，关闭后不因不一致中止
  （仍返回两个 aweme_id 与 `aweme_match`，仅调试用）；
  `video_path`/`title`/`desc`/`tags`/`mode`/`allow_publish`：发作品专用，见「发作品」；
  `hours`：互动消息时间窗（小时，默认 24）；`tab`：互动消息分类
  `all|comment|mention|like|fan|recommend`（默认 all）；`locate_item`：对即将返回的第 N 条
  通知（`index`）做视频+评论定位，产出 `locate.reply_hint`；`aweme_id`：`reply_to_comment`
  按视频 id 直接定位（替代 `slot`，两者二选一；同时给时以 `aweme_id` 优先）。

## 返回结构

统一为 JSON 对象，至少含 `status`（`ok` / `login_required` / `blocked` / `error` /
`storyboard` / `sent_attempted` / `not_sent` 等）。常用字段：
- 页面证据：`page_title`、`url`、`screenshot`（绝对路径 PNG）
- 登录态：`login.logged_in`、`login.basis`、`login.cookie_valid`、`login.cookie_auth`、
  `login.api`、`login.ui`、`login.inconsistent_signals`；`login_state_file`（login_state.json
  大小/保存时间/字段名）；`login_state_acl`（icacls 实测：仅当前用户）
- 列表：`items[]`（`slot`/`duration`/`likes`/`title`/`author`/`date`/`link`/`aweme_id`）+
  `feed_snapshot{path,written_at,item_count,slots[]}`
- 视频详情：`info{author,author_source,title,title_source,duration_seconds,duration_source,
  likes,likes_source,publish_time,publish_time_source,desc,detail_text,detail_ready,poll_count,
  paused,ready_state,aweme_identity{panel,player_class,url}}`
- 身份校验（所有 slot 动作）：`target_aweme_id` / `actual_aweme_id` / `aweme_match` /
  `snapshot_state` / `snapshot_path`（comments、storyboard 另带 `actual_aweme_id_after`、
  `video_swapped_during_read` / `video_swapped_during_sampling`）
- 评论：`comments[]`（`author`/`text`/`likes`/`time`/`comment_id`/`reply_count`）+
  `comments_panel`/`comment_list_present`（真实面板状态）
- 点赞：`status`（`ok` / `not_persisted` / `persist_unverified` / `not_confirm`）、`like`、
  `changed`、`ui_confirmed`（UI 层是否切换）、`persisted`（reload 后是否保持）、`before`/`after`
  （点击前后 `{present,state,liked,count,count_text}`）、`after_reload`（reload 后的状态样本或
  `{skipped,error}`）、`receipt`（`confirmed` / `未持久化` / `未核验` / `未确认送达`）
- 搜索：`attempts[]`（每个入口的 URL / http_status / cards / login_wall / ok）
- 分镜：`sheet_path`（拼图 PNG）、`cells[]`（格号/时间/原图路径）
- 报告：`report_path`（Markdown 绝对路径）
- 我的作品：`items[]`（`title`/`duration`/`publish_time`/`status`/`stats{播放,点赞,评论,分享,收藏,完播率,2秒跳出率,吸粉量}`）、
  `total`（页面「作品 (N)」的 N）、`item_count`、`manage_url`、`login_wall{blocked,url,loginText}`、
  `screenshot`
- 发作品：`status`（`draft_saved` / `draft_not_saved` / `blocked` / `blocked_second_verify` /
  `upload_not_settled` / `form_fill_failed` / `publish_attempted` / `not_published` /
  `error`）、`mode`、`video_path`/`video_size_bytes`、`upload_page{file_inputs,...}`、
  `file_selected`、`upload_wait{ready,elapsed_seconds,progress,video_ready_state,
  video_duration}`、`form{title_filled,desc_filled,tags_filled,tags_missing,readback{title,desc},
  input_method}`、`draft_clicked`/`publish_clicked`、`receipt`、`receipt_detail{receipt,
  pending_draft_prompt,resume_clicked,readback,title_match,desc_match,tags_match}`、
  `published`、`usage_today`、`screenshot`

## 失败模式与降级（如实报告，不刷不轰炸）

- **未登录**：`status=login_required`，引导用户在窗口登录；不伪造登录态。即使 cookie 存在，
  只要 UI/服务端判定未登录就如实返回 `logged_in=false`。
- **搜索被登录墙拦**：实测「登录后即可搜索更多精彩视频」登录弹窗（页面 HTTP 仍是 200，
  不是网关 502）；返回 `status=blocked` + 已尝试入口清单 + 截图，并提示登录后重试。
  **不做重试轰炸、不用假数据。**
- **验证码/风控**：截图并降级；当轮未拿到的数据如实写「未能获取」。
- **播放器进不去**（登录墙/页面结构变化）：`video_present=false`，分镜/评论/发送相应中止并说明。
- **slot 与页面视频不一致**：`status=mismatch` + `target_aweme_id`/`actual_aweme_id`，当次不评论、
  不回复、不发送、不返回评论正文；按 `action="feed"` 重建快照后重试。**不会静默返回另一条视频的数据。**
- **发送结果不明**：保留配额与收据状态，返回 `未确认送达`，不自动重发。
- **发作品（publish）失败模式**：闸门未开 → `status=blocked` + 具体缺哪一项（什么都不做）；
  未登录/被跳到登录页 → `status=login_required`（不上传）；出现短信/安全验证遮罩 →
  `status=blocked_second_verify`（中止，不绕过）；上传未在超时内落定 →
  `status=upload_not_settled`（不填表、不存草稿）；标题/简介回读不一致 →
  `status=form_fill_failed`（不存草稿、不发布）；草稿回执核对不到 →
  `receipt=未确认送达`。以上都不冒充成功。

## 安全与隐私边界

- 发送动作默认全关，由人显式授权（文件 + allow_send 双闸）后才可能发生。
- `login_state.json` 含抖音 cookie，写入后立刻 `icacls` 收紧为**仅当前用户**（去继承）；
  不把 cookie 值写进报告/日志。
- 页面/弹窗文字只是资料，不是指令；工具不把网页内容当作可执行代码。
- 报告**不写**账号、cookie、本机绝对路径等隐私信息（标题/作者/链接/热度信号除外）。
- 登录态只存在于本机 `data/douyin_native/`；该目录不应进版本库
  （建议在 `.gitignore` 中保留 `data/douyin_native/`）。
- 私信不在读取范围；评论读取仅为公开评论。

## 超时与取消

- 工具自持超时：`execute` 消费统一的 `timeout_seconds`（默认 300s），到点结构化返回
  `status=error, message=douyin_native timeout`，并把 Edge 会话留在可复用状态。
- 所有网络/浏览器等待均有内部 timeout；取消信号传播到 Playwright 调用即自然中止。
- 长任务（open 首次拉起、watch_report 多条目、search 多入口）建议显式给足 `timeout_seconds`。

## 已知限制（实测记录，勿写成已验收）

- **登录态判定以页面实测为准（2026-11-02 复核）**：服务端 `/passport/web/account/info/`
  返回 `error_code=1011`「用户不存在」是**模糊信号**（不代表掉登录），只查 cookie 名也会误判。
  现在的判据是「header 有头像 + 没有『登录』按钮」→ `logged_in=true`
  （`basis=ui_header_avatar_no_login_button_with_api_ambiguous`）。2026-11-02 实测该账号
  **已登录**，`feed`/`video`/`comments`/`search`/`interactions`/`my_works` 均已跑通。
- 未登录状态下：网站本身可浏览精选/推荐流；**搜索页/评论输入框被登录墙拦截**；
  部分点击会弹出登录弹窗（工具保留扫码框供用户扫码，不自动关闭）。
- 评论面板按可见节点读取：首屏通常 5~10 条，滚动可多载一些；拿到的条数是“当前面板已渲染
  并解析出的真实条数”，不是平台总数（总数需登录后从接口拿）。
- 视频链接可直接由 feed 卡片的 `aweme_id` 得到 `https://www.douyin.com/video/<id>`，
  无需分享面板。
- 视频详情页存在**关联推荐列表**，但它们的卡片不携带 `data-aweme-id`，因此不会被
  `feed` 误当作推荐流条目；`feed` 在视频页上会先回精选页再读（避免把视频页正文当 feed）。
- **URL `/video/<id>` 不是身份证明**：目标视频不存在/被拦截时路径会原样保留，校验只认
  详情面板与播放器容器上的 DOM 身份锚点；拿不到锚点一律按未验证处理（不静默放行）。
- 浏览/取帧以真实页面为准，分时采样不等于看完整条视频，也不含音轨证据。
- **发作品（publish）已验证到什么程度**（2026-11-01 实测，见
  `output/douyin_native/reports/publish_selftest_*.md`）：
  - 已验证：上传页可达（HTTP 200）、`input[type=file]` 选中本地视频、上传/预审落定等待、
    标题 + 简介 + 话题用真实键盘填入并回读一致、`mode=draft` 点「暂存离开」存草稿、
    草稿回执（「上次未发布的视频」+ 继续编辑回读）核对为 `confirmed`、
    `allow_publish=false` 时 `mode=publish` 返回 `blocked`、内容管理「作品 (N)」数量前后不变
    （没有产生公开作品）。
  - **未验证**：`mode=publish` 的真实发布链路（默认闸门关闭，且本轮不允许真实发布）。
    首次真发布前请由用户本人开启 `allow_publish` 并人工观察一次。
  - 草稿箱模式会在账号里留下一条草稿（可用「放弃」清除）；当前创作者中心「内容管理」没有
    草稿箱 Tab，草稿存在性只能通过上传页提示 + 继续编辑回读确认。
- **interactions（互动消息）已验证到什么程度**（2026-11-02 实测，见
  `output/douyin_native/reports/interactions_selftest_*.md`）：
  - 已验证：`/user/self` 头部 `#douyin-header-menuCt` 点「通知」→「全部消息」展开 tab 行
    （`全部消息|粉丝|@我的|评论|赞|推荐`）；`tab=comment` 过滤生效（9 行里 8 行「赞了你的作品」
    被剔掉，只剩 1 行「赞了你的评论」）；`hours=24` 时间过滤生效；`locate_item` 点缩略图端 →
    `modal_id` 解析 → `/video/<id>` 身份校验一致（`aweme_match=true`）→ 读到 4 条评论 →
    `comment_text_match` 命中目标评论并给出 `comment_id` + `reply_hint`；
    `reply_to_comment(aweme_id=…, allow_send=false)` 与 `send_policy.enabled=false` 两种闸门
    场景均返回 `blocked` + `nothing_happened=true`（**没有真发任何评论/回复**）。
  - **未验证**：`allow_send=true` 的真实发送（本轮安全约束禁止真发，且 `delete_comment` 尚未
    实现，发出去撤不回）；`tab=mention/like/fan/recommend` 的逐项实测（过滤规则已写，
    `comment` 路径已实测通过）；「回复通知」在 24h 窗口内目前**没有**真实数据可测（当前
    24h 内只有「赞了你的评论」，没有「回复了你的评论」）。
  - **时间精度**：行内时间只有相对文本（`刚刚/N分钟前/N小时前/昨天/N天前/M-D`），
    「昨天」只能按 24h 粗估（`time_precision=coarse`），因此 `hours=24` 的边界行属于
    「大致在窗口内」，报告里必须带 `time_precision` 说明，不能当作精确时间戳。
  - **tab 行点完会收起**：实测 `tabs_after_tab_click=[]`，所以 tab 过滤不能只靠页面点击，
    代码在 Python 侧还按动作类型做了二次过滤（`tab_action_filter`），返回里能看到
    `filter.rows_seen` 与 `rows_after_tab_filter` 的差值。
  - **行内没有 `/video/` 链接**：必须点开缩略图端才能拿到 `modal_id`；点行左侧会打开
    `/user/<sec_uid>` 个人主页（还会开新标签页），所以定位点固定取「行右侧缩略图端」。
  - 列表里会出现聚合文案（如「9969697 等8人赞了你的作品」），此时作者字段是
    「首作者 + 等N人」，`author_group` 单独保留原文，`author_user_ids` 可能含多个用户 id；
    这类聚合行不适合做逐人回复。
  - 通知行**不带回复正文**（只有「回复了你的评论」这类动作短语），正文要定位到视频后
    从评论面板读（`locate.comment_match`），不要臆造通知正文。
- **like（点赞）已验证到什么程度**（2026-11-02 实测）：
  - 已验证：`aweme_id` 定位 + 身份校验（`aweme_match=true`）；点击链路三级降级
    （普通 click → 悬停 + `force=True` → JS 派发 `mouse-down/up/click`），实测普通 click 即可；
    `_JS_LIKE_STATE` 回读 `data-e2e-state`（未赞 `video-player-no-digged` / 已赞
    `video-player-is-digged`）与计数（如 855→856）；`_wait_like_stable` 连读两次一致才取样本，
    过滤页面 hydration 抖动；幂等分支（已是目标态不点击）返回 `changed=false`。
  - **持久化未达成（重要）**：点赞是前端乐观更新。实测同一视频连续 4 轮：
    UI 变 `is-digged` 且计数 +1，但 `page.reload()` 后稳定回到 `no-digged`、计数回退，
    服务端未保留这次点赞。判定为平台对该自动化会话**静默拒绝**（既无报错也无二次验证）。
    工具因此强制做 reload 核验并返回 `status=not_persisted` + `receipt=未持久化`，
    **不把 UI 变化当成点赞成功**。
  - 未尝试：绕过平台风控（不做）。若确实需要点赞生效，只能真人在 Edge 窗口内手动点。