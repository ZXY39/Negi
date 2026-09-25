# Fix Plan: 已安装设备的自动版本检查（每 5 小时 + 启动读台账）、设置按钮红点、一键「重启并更新」，以及 release 作为国内备用入口

> Origin: operator request, 2026-09-24 —— 「项目启动后一定时间（例如每 5 小时）自动检查新版本；不自动更新，但要提醒用户；设置按钮左上角出现小红点；点进设置能手动检查并更新到新版本；启动时命令行也提醒」。**操作员当场推翻并替换了三条既定约束**：① `docs/FIX_PLAN_one-line-install.md` §5 的「不做启动时自动检查 / Web 版本横幅」作废；② 「不自动更新」收窄为「不自动*换代码*，但用户点一次后可以自动完成退出→升级→重拉起」；③ 「只打 tag、不发 release」改为**允许发布 release**，用途限定为国内备用下载通道。
>
> 三项歧义已由操作员裁决：**红点落位** = 侧栏底部「设置」按钮左上角（原话的「标题右上角设置按钮」在本项目不存在，见 §1.3）；**应用深度** = 一键「重启并更新」；**README 主入口** = 保持 `raw.githubusercontent.com`，release 资产只作备用通道。
>
> Status: P1–P9 已实现。彩排里抓出并修掉两处会真出事的缺陷（执行体漏传 `-Dir` ⇒ 升级到别的目录；端口回落到默认值 ⇒ 可能关同机另一个实例），见 §4。跨版本的"有新版→一键更新"实盘仍待下一个 tag。
>
> Scope: `g3ku/update_check.py`（扩）、`g3ku/config/schema.py` 新增 `update_check` 分区、`g3ku/shells/web.py` 的 60 秒对账循环、`main/api/` 新 router、`g3ku/web/frontend/`（org_graph.html / org_graph_app.js / org_graph_redesign.css）、`g3ku/web/launcher.py` 启动横幅、`.github/workflows/`。

---

## 0. 决定形状的四条事实

| # | 事实 | 证据 | 决定 |
| --- | --- | --- | --- |
| F1 | 跨进程维护卡权存在但不适合这里 | `main/storage/sqlite_store.py:1031` `claim_maintenance_run(...)` 绑在主运行时 SQLite 上 | 版本检查是每设备一次的轻量读，为它把 web 的 60 秒循环耦合到 `main_task_service.store` 不值；**改为台账自带 `checked_at` 时间戳把关间隔** |
| F2 | web 进程已有一条 60 秒周期循环，且带「每 N 轮做一次」范式 | `g3ku/shells/web.py:709` `_outbox_reconcile_loop`；范式同形于 `OUTBOX_BRIDGE_SYNC_EVERY_N_CYCLES = 5`（`web.py:86`）；sleep-first + 单轮异常不杀循环 + `web.py:1143` 显式取消顺序 | **不新建循环**，加 `UPDATE_CHECK_EVERY_N_CYCLES = 300` |
| F3 | 该循环只在项目解锁后才起 | `_ensure_outbox_reconcile_running` 早退于 `_global_bus is None`（`web.py:741`） | 锁定态天然不联网检查 ⇒ 隐私默认免费成立，不需要额外开关 |
| F4 | 运行中替换源码有过事故 | worker 在 `0ea7ec72` 的编辑窗口 import 到半截文件 ⇒ `submit_next_stage` 18 连败 `NameError`、阶段死锁（项目记忆 `project-poisoned-module-stage-deadlock`） | apply 绝不能在 web/worker 存活时 `git checkout`；必须进程外，且顺序是「先确认无在跑工作 → 优雅退出 → 升级 → 重拉起」 |

补充：优雅退出与"有没有在跑的活"都已有实现 —— `POST /api/bootstrap/exit`（`main/api/bootstrap_rest.py:432`，`pause_running_work` 语义、无确认时 409 `running_work_requires_confirmation`）、`GET /api/bootstrap/exit-check`（`:421`）。**apply 不复制这套关停逻辑**：它只把执行体踢起来，执行体再走 `/api/bootstrap/exit`，所以"有在跑的活未确认"的 409 全项目只有一份实现。

---

## 1. Current State (verified at HEAD `4648db1a`)

### 1.1 已有
- `g3ku/update_check.py`：`fetch_latest_release_tag(project_root, timeout=2.0)`（`git ls-remote --tags origin`，只收 `refs/tags/vX.Y.Z` 形状，滤掉 `backup/*` 与 peeled `^{}`）、`parse_version()`。只出不进、失败返回 None。
- `g3ku status` 的 `Release:` 行（`g3ku/cli/commands.py` `_print_release_status`）—— 手动、联网、无缓存。
- 安装器 `-Upgrade`（`install.ps1` / `install.sh`）已经能做真正的代码升级，包括脏树拒绝与 `.venv`/`.g3ku` 保护。

### 1.2 没有
台账文件、任何周期性检查、任何 `/api/update*` 端点、前端红点与面板行、启动时的版本提醒、release 资产。

### 1.3 前端落点的实测形态
- 设置按钮：`g3ku/web/frontend/org_graph.html:123` `#project-settings-btn`，容器 `.sidebar-footer`（flex row，`org_graph_redesign.css:363`），兄弟只有 `#theme-toggle`（`:119`）。点击绑定 `org_graph_app.js:15182` → `openProjectSettingsDialog()`（`:10048`）。
- **该按钮没有 `position:relative`**（`org_graph_redesign.css:377-401`）⇒ 直接加绝对定位的红点会挂到别的祖先容器上（本项目反复踩过的"定位容器≠可见盒"坑）。
- 角标先例：`org_graph.html:113` 静态 `<span id="audit-nav-badge" class="nav-badge" hidden>` + `renderAuditNavBadge()` 切 `hidden` 属性与 `textContent`（`org_graph_app.js:14558`）；绝对定位变体见折叠态规则 `org_graph_redesign.css:346`。
- 设置面板是**纯 HTML 模板**（`org_graph.html:1006-1034`），不是 JS 拼 DOM；"按钮 + 一行结果文本"的现成形状是 `#config-bundle-import-pick-btn` + `<p id="config-bundle-import-name">未选择文件</p>`（`:1102-1104`）。
- 前端已有 30 秒角标轮询节拍（`AUDIT_BADGE_POLL_MS = 30000`，`org_graph_app.js:59`；`bindAuditBadge()` `:14601`）⇒ 不新建定时器。

---

## 2. Design

### 2.1 台账（唯一事实源）
`.g3ku/update-check.json`：`{checked_at, current_version, latest_tag, newer, source: "auto"|"manual", error}`。写在数据目录（`.g3ku/` 已 gitignore），读的一侧**永不联网**：
- `read_update_ledger()` 返回最近一次结果或 None。
- `run_update_check(store, *, source)`：先 `claim_maintenance_run('update_check', interval*3600)`（auto 走卡权，manual 强制绕过），再 `fetch_latest_release_tag`，成功/失败都落台账。
- 台账缺失 = 「还没查过」，前端与 CLI 都**不显示任何东西**；绝不把"没查过"渲染成"已是最新"。

### 2.2 调度
`g3ku/shells/web.py`：`UPDATE_CHECK_EVERY_N_CYCLES = 300`（300 × 60s = 5h），在 `_outbox_reconcile_loop` 里按既有 `cycles % N == 0` 范式起一次 `await asyncio.to_thread(run_update_check, source="auto")`。间隔由 `config.update_check.interval_hours` 换算，改配置后下一轮自然生效（热刷新已支持任意新字段，`g3ku/runtime/config_refresh.py` 整份 Config 驱动）。
启动即查一次：`_ensure_outbox_reconcile_running()` 起循环时先跑一次（sleep-first 的例外由台账卡权兜住间隔，不会每次都联网）。

### 2.3 配置分区
`g3ku/config/schema.py:987-997` 根部加 `update_check: UpdateCheckConfig`，字段 `enabled: bool = True`、`interval_hours: float`（默认 5.0，夹在 0.1–24）。模板 `g3ku/templates/config.example.json` **不加**这一节 —— `stt` 也不在模板里，缺节走默认已是既有范式。不做 `auto_apply` 开关：apply 永远要用户点一次，留一个没人消费的字段只会让人以为它能开。

### 2.4 API
`main/api/__init__.py` 加 `update_router`。三个端点全部走已有 423 锁定守卫（**不加**进 `/api/bootstrap` 白名单 —— 锁定态不该有版本 UI）：
- `GET /api/update/status` → 读台账 + 本地版本，零网络。
- `POST /api/update/check` → 立即复检（manual，绕卡权），回写台账并返回。
- `POST /api/update/apply` → 见 §2.5。body 可选 `{"ref": "v1.0.2"}`，默认台账里的 `latest_tag`。

### 2.5 apply = 进程外的一次性重启并更新
顺序（每步失败都要留痕并能人工接手）：
1. `GET`-式预检复用 `_running_work_snapshot()`：有在跑的会话/任务且未带 `pause_running_work=true` → **409**（沿用 `bootstrap_rest.py:441` 的 `running_work_requires_confirmation` 语义与中文文案）。
2. 起一个**脱离父进程**的执行体：`subprocess.Popen([sys.executable, "-m", "g3ku.update_apply", "--ref", ref], creationflags=DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP | start_new_session=True)`，日志写 `.g3ku/logs/update-apply.log`。
3. 本进程调 `shutdown_web_runtime()` + `request_server_shutdown()`（与 `/api/bootstrap/exit` 同一条收尾路径，不新造）。
4. 执行体侧：轮询等 `.g3ku/start.lock` 释放且端口空出（上限 90 秒）→ 跑 `install.ps1 -Upgrade -NoStart` / `install.sh --upgrade --no-start`（同目录那份，带 git 时走 fetch+checkout，脏树会被拒绝并把原因写日志）→ 成功后重新拉起 web（`-m g3ku web`）→ **失败则原样重新拉起旧版本**，绝不把设备留在"没有服务"的状态。
5. 前端：点「重启并更新」后提示"服务将重启，约 x 秒后自动重连"，靠已有的重连车道回到页面；红点在台账 `newer` 变假后自行消失。

### 2.6 前端
- `#project-settings-btn` 补 `position:relative`；里面加静态 `<span id="update-nav-badge" class="update-dot" hidden>`，红点绝对定位在按钮**左上角**（`top/right` 负偏移），尺寸与配色走 V2 现有 token，不引入新色值。
- 在既有 30 秒节拍里追加一次 `GET /api/update/status`：`newer=true` → 去掉 `hidden`；台账缺失或 `error` → 保持隐藏。
- 设置面板加一节：`当前版本 v1.0.1 / 最新标签 v1.0.2 / 上次检查时间` + 「检查更新」按钮 + 一行结果文本（照 `#config-bundle-import-*` 那对元素的形状）；发现新版时多一个「重启并更新」按钮，点击先弹一次确认（列出在跑会话/任务数），确认后调 apply。
- 文案克制：面板里不放解释性段落，控件自述。

### 2.7 CLI 启动提醒
`g3ku/web/launcher.py:247 run_web_server_entrypoint` 起服务前读台账（不联网），`newer=true` 时打一行 `[g3ku] 发现新版本 v1.0.2（当前 v1.0.1），在网页设置里点「重启并更新」或跑 install -Upgrade`。台账为空则不打。

### 2.8 release 作为备用通道
`.github/workflows/` 加 tag 触发的 job：`gh release create`（或 `softprops`）把 `install.ps1` 与 `install.sh` 作为该 tag 的资产上传。README 主入口仍是 `raw.githubusercontent.com`，其下补一条「raw 解析不了就用 release 资产：`https://github.com/ZXY39/G3KU-Agent/releases/download/v1.0.2/install.ps1`」。**不改**版本检查通道（ls-remote 已够，且 `releases/latest` 会引入 API 配额依赖）。

---

## 3. Phases

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| P1 | 台账读写 + `claim_maintenance_run` 卡权 + 配置分区 | `g3ku/update_check.py`、`g3ku/config/schema.py`、模板 |
| P2 | 挂进 60 秒循环，含启动首查 | `g3ku/shells/web.py` |
| P3 | 三个端点 + router 注册 | `main/api/update_rest.py`、`main/api/__init__.py` |
| P4 | `g3ku/update_apply.py` 执行体（脱离进程、等待释放、升级、失败回拉旧版本） | 1 个新模块 |
| P5 | 红点 + 面板节 + 30 秒节拍复用 + `node --test` 用例 | 前端 3 文件 + 1 测试 |
| P6 | 启动横幅读台账 | `g3ku/web/launcher.py` |
| P7 | release 资产 CI job + README 备用通道 | `.github/workflows/release.yml` |
| P8 | 文档：`web-and-admin.md`（新端点的 owner）、`operations-and-maintenance.md`、`docs/architecture/README.md` 指针、README | 4 处 |

---

## 4. 验收

1. 单测：台账缺/在/过期三态、`newer` 计算、manual 绕卡权而 auto 受 5 小时间隔约束（对 `claim_maintenance_run` 打桩或直接跑真 SQLite）。
2. API：`GET status` 在未检查时返回空且**不联网**；`POST check` 后台账落盘；`POST apply` 在有在跑工作时 409。
3. 前端：`node --test` 覆盖红点的 `hidden` 翻转（沿用 `tests/resources/org_graph_app.audit_badge.test.js` 的桩 harness，注意桩的 `classList` 是空实现，断言类名要换 Set 记录器）。
4. apply 车道的实盘（在仓库外的隔离副本 + spare 端口，绝不碰在跑的 127.0.0.1:18790）：`update_apply` 走完整序列 **退出请求 → 等端口释放 → 升级 → 重新拉起**，且拉起的服务真能再应答 `/api/bootstrap/status`。三条中止路径同样实测：未解锁 `423`、未配置运行面 `500`、端口仍被占 ⇒ 全都"不碰代码"直接退出。
   - 这一跑抓出两处缺陷并已修：① 执行体没把项目目录传给 `install`，脚本退回默认 `%USERPROFILE%\G3KU-Agent`，日志里"升级成功"其实是升级了另一个目录、重启的仍是旧代码；② 端口原先从配置文件猜、回落默认 18790，等于可能去关同机另一个实例 —— 现在由调用方显式传入，拿不到就中止。
5. 端点实盘：`POST /api/update/check` 在无 git 的树里返回 200 且 `error=remote_unreachable`；伪造台账为 `newer=true` 后 `GET /api/update/status` 如实回显；`apply` 对 `v9;evil` 返回 400 `invalid_ref`；锁定态三个端点一律 423。
6. 前端：`node --test` 全量 429 项，新增 3 项全过，唯一红项 `区分线两端留白…` 在 HEAD 干净 worktree 里同样红（存量，非本次引入）。
6. ruff + `tests/resources/test_resource_runtime_smoke.py` + 本次相关测试；不跑全量（本仓库全量非绿）。

---

## 5. 明确不做

| 不做 | 原因 |
| --- | --- |
| 静默自动换代码（不需用户点） | 操作员要的是"提醒 + 一次点击"；且 F4 事故说明运行中换源码的代价不可接受 |
| 把 `/api/update/*` 加进锁定白名单 | 锁定态不该有版本 UI，也不该有外网行为（F3） |
| 前端自建新的定时器 | 30 秒节拍已存在（§1.3），多一条循环只是多一份抖动 |
| 改用 GitHub API 做版本通道 | 未鉴权 60 次/小时/IP；ls-remote 已实测可用 |
| 自动回滚上一次升级 | apply 失败只保证"旧版本能被重新拉起"，不做代码级回滚 |
| 检查时上报任何本地信息 | 只 GET 标签列表；版本号也不外发 |
