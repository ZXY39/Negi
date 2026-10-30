# 沙箱执行面（信任分级 / 受限档 / 读根限定）

本层回答一个问题：**这一轮是谁在要求运行时做事，它因此能触到哪些东西？**
会话本身不带答案——QQ 群里的陌生人和操作员本人在转录里长得一模一样——所以答案由
三样输入解析出来：会话信任级、会话档位意图、沙箱安装态。

边界划分沿用"控制面 / 数据面"：

| 面 | 内容 | 执行位置 |
|---|---|---|
| 数据面 | 命令、文件读写、内容读取、进程控制、网络抓取 | 宿主（`host_full`）/ 宿主进程内受限（`limited`）/ jail（`jailed`，后续阶段） |
| 控制面 | 记忆写入、任务与节点、cron、模型配置、治理 | 恒在宿主，由档位判定准入 |

判据只有一个实现：`g3ku/sandbox/policy.py`（档位与工具面）与
`g3ku/runtime/session_trust.py`（信任级）。前门执行束、曝光层、节点执行道、
记忆门、QQ 桥都读这两份，不各自再判一次来源。

## 档位模型（意图 × 安装态）

会话只存**一档意图**（`Session.metadata["sandbox"]["mode"]`），实际执行面由**安装态**
解析，档位本身不入库：

| 安装态 | `restricted` | `unrestricted` |
|---|---|---|
| `not_installed` / `runtime_missing` / `partial`（该信任档必需维度不全） | `limited` | `host_full` |
| `ready` | `jailed` | `host_full` |

- **默认意图按信任派生**：`third_party*` → `restricted`，`owner` → `unrestricted`。
  操作员本地会话因此保持既有行为（宿主执行 + 现有 guards + 记忆注入 + 可派任务）。
- **装/卸沙箱不改任何会话元数据**：意图不动，档位随安装态变；`restricted` 会话在
  安装后自动升 `jailed`，卸载后自动回 `limited`。
- **`partial` 只服务 owner**：`third_party` 的必需维度（`fs_read` / `fs_write` / `network`）
  缺一维就不给它 jail——把"缺一维"当"全缺"或反过来，两种都是假安全。
- 档位变化**只在回合边界生效**，不打断在飞回合。
- 空会话键（无会话上下文的装配、CLI、检查实例）按 `host_full` 处理：外部渠道的每一轮
  都带 `ext:` 键，走不到这一支；按第三方处理会让所有无会话的装配路径失去工具。

本层解析出 `limited` 与 `host_full`；`jailed` 的分支在 `resolve_tier` 里已经写好，
但要等 jail 后端（manager + router）接进执行道、`JAIL_BACKENDS_AVAILABLE` 打开之后才会发放。
在那之前工具面与读根判据已经就位，接后端只改这一处开关。

## 头部沙箱声明

模型必须知道自己站在哪一面。不告诉它，它就拿宿主路径、宿主挂载和联网命令去试，每撞一次
赔一个回合——验收第 4 条里模型先 `cat /mnt/e/...` 再 `content_open E:\...`，两次都是猜的。

稳定前缀里因此有两段声明，按解析出的档位选一，由 `CeoPromptBuilder.build_base_prompt`
渲染：

| 档位 | 头部段 |
|---|---|
| `jailed` | `## Sandbox Environment`：一次性 bwrap、只有 `/workspace` 与 `/shared` 落盘、宿主路径不存在、`network=none` |
| `limited` | `## Limited Session`：会话工作目录可写、CLI 默认不可用的边界、读根/写根限定 |
| `host_full` | 不带声明 |

**档位进 cache stamp**：重印时钟是（记忆块采纳时刻 × 工具面暴露修订 × 档位）。切档会
立即重印 system prompt、`tools[]` 与 pinned contract——旧声明留在下一回合会让模型按旧工具面
行动，安全面与能力面都会错。代价是每次显式切档一次完整前缀 cache miss，这是操作员主动
动作，接受。渠道会话的头部渠道块同步追加当前模式与限制；`host_full` 不写模式段。
pinned contract 同时带 `session_mode` 与受限档的 `session_workspace_dir:` 绝对路径；变化
经 `[RUNTIME_NOTICE]` 的 `### Session mode` 节投递一次（首次只对齐，不重复头部已有内容），
工具列表变化也复用同一条通知。压缩跳后新头部以当刻值重钉。

## jail 基础设施（后端 / 布局 / 助手 / 文件桥）

这一层已经就位，但**还没有接到执行道**：`JAIL_BACKENDS_AVAILABLE` 仍为 false，
所以没有会话会进 `jailed`。接线的最后一步（把数据面工具换成代理实例）与真机验收
是同一件事，验收没跑完就不打开闸门。

| 模块 | 职责 |
|---|---|
| `g3ku/sandbox/backend_base.py` | 后端契约：`SandboxSpec`（卷、可写根、网络、助手绑定）、`Enforcement`（逐维度）、`ExecResult` |
| `g3ku/sandbox/profiles.py` | **bwrap argv 与 Seatbelt profile 的唯一产地**：`--unshare-all`、`--clearenv`、最小只读系统根、会话卷唯一可写、`/mnt` 与 `/init` 永不 bind；`assert_profile_safe` 在执行前再扫一遍 bind |
| `g3ku/sandbox/backend_bwrap.py` | Linux：拼 argv + 执行前断言 |
| `g3ku/sandbox/backend_wsl.py` | Windows：`wsl -d <distro> -- bwrap …`；宿主环境原样传给 `wsl.exe`，jail 内环境由 bwrap 决定 |
| `g3ku/sandbox/backend_seatbelt.py` | macOS：`sandbox-exec -p <SBPL>`，默认全拒 |
| `g3ku/sandbox/backend_container.py` | 可选：`cap-drop=ALL`、`no-new-privileges`、pids/内存上限、inbox 只读挂载 |
| `g3ku/sandbox/fsop.py` | jail 内一次性文件助手（read/write/replace/list/stat/search），只依赖标准库、走字节流 JSON |
| `g3ku/sandbox/manager.py` | 每会话一个卷、lease 计数、idle 回收、执行与 fsop 委派；本地卷在数据根，WSL 卷在发行版内 |
| `g3ku/sandbox/bridge_files.py` | `/shared/{inbox,outbox}` 导入导出：只收普通文件、拒 symlink、文件名单段化、64 MB 上限 |
| `g3ku/sandbox/router.py` | 数据面工具 → jail 代理：`exec` 走 `manager.run`，`filesystem_*` / `content_*` 翻成 `fsop`；只覆写 `execute`，契约成员全部转发（`tools[]` 字节不变） |
| `g3ku/sandbox/errors.py` | `denied` / `unavailable` / `runner_failure` 三分类（按后端 stderr 签名，不猜模型文本） |

代理的三条行为约束：路径先过 `display_path`（模型只该看见 `/workspace` 与 `/shared`，
宿主绝对路径直接拒）；`exec` 的 `background` 在 jail 里拒绝（一次性进程，后台活不过这次调用）；
所有阻塞调用走 `asyncio.to_thread`，不占事件循环。`filesystem_propose_patch` 与
`process_kill` 不进 jail 代理——前者依赖宿主工件库，后者在一次性进程模型下没有对象可杀。

几条实现约束值得记住：

- **`fsop` 靠单文件绑进 jail**（`/opt/negi/fsop.py`，只读）：jail 里没有仓库、不装包，
  标准库足够。WSL 布局把这份文件拷进发行版一次，但它仍然只是一条 bind 的**源**——
  `python3` 是在 jail 内执行的，发行版路径在 jail 内不存在，所以两个后端都走同一条
  `helper_binds` 契约，`_fsop_helper_for` 也一律回 `FSOP_JAIL_PATH`。
- **jail 里有什么，由后端回答，不由宿主 stat 回答**：`build_bwrap_argv` 的
  `path_exists` / `path_is_file` 决定绑哪些系统根、补哪些 merged-usr 软链、助手文件在不在。
  Linux 后端上宿主就是 jail 的那一侧，默认判据即正确；WSL 后端的宿主是 Windows，
  `Path("/usr").exists()` 恒为 False，于是必须问发行版本人，否则 profile 产出一个只绑了
  会话卷的空根，`exec` 回执是 `bwrap: execvp sh: No such file or directory`。问不到发行版
  时后端拒绝拼 argv，而不是交出一份"看着像绑全了"的 profile。
- **`wsl.exe` 用 `-e` 而不是 `--`**：`--` 把剩余参数拼回一条命令交给发行版的默认 shell，
  于是模型命令里的 `$( … )` 与 `$HOME` 在 **jail 外**求值——同一次调用里 `pwd` 给出
  `/workspace`，`ls /` 却报出发行版整根、`ls /mnt` 数出宿主盘符。探测与数据面两条车道都吃
  这条；bwrap 自己那个隔开 profile 与 jail 命令的 `--` 不受影响。
- **jail 内不继承宿主环境**：本地后端由 `--clearenv` 与子进程净化环境双重保证，
  `G3KU_*` 一个都进不去；`wsl.exe` 自己需要宿主环境启动，这一层不过 jail 边界。
- **装配接线**：`tier == jailed` 时执行束把数据面实例换成代理；换不上（后端挂了、卷建不起来）
  就换成"永不执行"的替身并记 `runtime_context["sandbox_unavailable"]`，绝不掉回宿主实例。
  执行闸门另有一道硬校验：jail 档里数据面工具若解析到宿主实例，直接拒绝。
- **闸门是环境变量 `G3KU_SANDBOX_ENABLE_JAIL`**（默认关）。代码写完不等于可以打开：
  先按下一节的清单在装好后端的机器上验收，再设它。

运维入口：`negi sandbox status|install|uninstall|persona`（`install`/`uninstall` 先打印计划，
必须 `--yes` 或交互确认）；`negi status` 的环境体检包含沙箱安装态、缺失的必需维度，以及
"第三方会话被放到全权限"的清单。

## 上机验收清单（打开闸门之前）

在装好后端的机器上（Linux `bubblewrap` / Windows 有合格发行版 / macOS 自带 `sandbox-exec`），
按顺序跑；任何一条失败就把 `G3KU_SANDBOX_ENABLE_JAIL` 留在未设置状态：

1. **安装态**：`GET /api/sandbox/status?refresh=1` → `install_state=ready`，
   `enforcement` 的 `fs_read` / `fs_write` / `network` 三项都是 `full`。
2. **开闸**：设 `G3KU_SANDBOX_ENABLE_JAIL=1` 重启 web；`GET /api/ceo/sessions/{id}/sandbox`
   的 `resolved_tier` 变成 `jailed`。
3. **根**：让模型 `exec pwd` → `/workspace`；`ls /` 里没有任何宿主目录树。
4. **读隔离**：`content_open` / `exec cat` 打 `.g3ku/master.key`、`config.json`、
   `memory/MEMORY.md` → 全部失败（不是"读到只读副本"）。
5. **写隔离**：写 `/workspace/x` 成功；`/etc`、`/home` 这类 profile 为了放 ro-bind 而
   合成的脚手架目录即便写成功也不算违规——判据是**宿主侧看不到**：到发行版里 `ls /etc/x`
   必须是 `No such file or directory`，写进去的东西随 mount namespace 一起消失。
   真正不许碰的是宿主路径（`/mnt`、`/root`、会话卷之外的一切）。
6. **网络**：`exec curl -sS https://example.com` 失败（`network=none`）。
7. **控制面不可达**：jail 内访问宿主 web 端口、容器运行时 socket、ssh-agent 全部失败。
8. **凭据**：`exec env` 里没有任何 `G3KU_*`、模型 key、master key、QQ AppSecret。
9. **无常驻**：jail 内 `ps` 没有 Negi 进程；宿主 `netstat` 没有为沙箱新增监听端口。
10. **WSL 额外**：`ls /mnt` 为空、`/init` 不存在、`exec notepad.exe` 失败。
11. **降级**：去掉 env 重启 → 群会话回 `limited`，`exec` 默认不可用（除非在会话工具列表显式勾选）。
12. **缓存**：同一会话切档前后 `tools[]` 字节不变，头部沙箱声明只在重印边界变。

## 档位存储与 API

- **意图存储**：`<数据根>/sandbox/session-modes.json`，进程内缓存、变更时整份原子落盘。
  换工作区不丢开关；每回合解析只读内存。`operator_unlocks` 同文件记"谁在什么时候用口令
  把会话放到 `unrestricted`"。
- **安装态**：`g3ku/sandbox/probe.py` 探测（Windows 扫已安装的 WSL 发行版、Linux 查 bwrap 与
  非特权 userns、macOS 查 `sandbox-exec`、任意平台回退容器运行时），结论按逐维度强制力
  上报并缓存在进程内；`POST /api/sandbox/refresh` 才重探。

  **Windows 不要求专有发行版**：`wsl -l -q` 列出的发行版逐个探能力，一个发行版要能承载
  jail 需同时有 `bubblewrap` 与 `python3`（后者跑 `fsop`）。专用发行版 `negi-sandbox`
  排在最前、命中即停，用户自己的发行版次之；最多探 4 个，避免多个发行版时启动被拖慢。
  有 Python 缺 bwrap 时给发行版内的安装命令（`runtime_missing` / `bwrap_missing_in_distro`），
  都不合格则 `no_qualified_distro`，一个都没有则 `wsl_distro_missing`。
  发行版开着 automount 不影响隔离——jail 的 mount namespace 里只有 profile 绑进去的路径，
  `/mnt` 与 `/init` 从不 bind；WSL interop 的 binfmt 用 `--unshare-pid` + 全新 `/proc` 挡在
  jail 外，探测把 `wsl_interop` 作为读数留给 doctor，不作为不合格项。

  **安装态是全局读数、不进会话元数据**；`jailed` 的发放另有一道
  `JAIL_BACKENDS_AVAILABLE` 闸门（见下）。
- **`JAIL_BACKENDS_AVAILABLE` 是 fail-closed 闸门**：jail 后端接进执行道之前恒为 False，
  `resolve_tier` 即便读到 `ready` 也只发 `limited`——安装态好看但没人看守的数据面不发。

端点（`/api` 前缀，与其它 CEO 会话端点同层）：

| 端点 | 契约 |
|---|---|
| `GET /api/ceo/sessions/{id}/sandbox` | 返回 `{trust, mode, stored_mode, default_mode, resolved_tier, tier_label, install_state, backend, enforcement, ui_labels, unlock_required, operator_unlock, tasks_allowed, writable_roots, allowed_tools, allowed_tools_customized, default_allowed_tools, fixed_tools, tool_warnings, exec_allowed, exec_requires_governed}` |
| `PATCH /api/ceo/sessions/{id}/sandbox` | 改意图：`{mode?, unlock_password?, writable_roots?}`；**任何信任级的开/关沙箱**（`mode` 与当前意图不同）都必须带项目解锁口令（`unwrap_with_password` 只读校验），通过后记 `operator_unlock` + 审计；`mode` 与原意图相同或只传 `writable_roots` 时不走口令闸。`writable_roots` 只对 owner 开放（D14：交付目录常在工作区外），third_party 传了直接 403；返回体带 `effective_from: "next_turn"` |
| `GET/PATCH /api/ceo/sessions/{id}/sandbox/tools` | 受限模式的按会话工具列表。GET 返回生效集合、默认集合、运行时固定项、逐工具 warning 与 `exec_allowed`；PATCH（`{allowed_tools, unlock_password}`）必须带项目解锁口令，写入 session state 并写 `sandbox_tools_changed` 审计；与默认集合完全一致时清掉 override。返回 `effective_from: "next_turn"` |
| `GET/PUT /api/sandbox/persona` | 第三方会话用的人设正文；空 = 群聊不带人设。与操作员自己的人设 note 分开存放 |
| `GET /api/sandbox/status` | 全局安装态、后端、逐维度强制力、`ui_labels`；`?refresh=1` 重探 |
| `POST /api/sandbox/refresh` | 重探并返回新读数 |
| `GET /api/sandbox/install/plan` | 安装计划（要执行的步骤、是否提权、是否可能重启、卸载命令） |
| `POST /api/sandbox/install` | `{confirm:true}` 才执行；后台线程跑步骤，失败即停 |
| `GET /api/sandbox/install/status` | 安装进度（步骤状态与输出尾部） |
| `POST /api/sandbox/uninstall` | 只注销本项目导入的专有发行版；系统包与用户发行版不动 |

独立页面：`/sandbox.html`（`g3ku/web/frontend/sandbox.{html,css,js}`），提供运行时状态与逐维度
强制力、安装计划的确认与进度、会话档位切换（含第三方口令闸门）、会话卷文件管理。
页面视觉复制外壳的 UI kit token（`--ui-*`：canvas / surface / border / brand / 控件档），
不加载 `org_graph.css`，所以改外壳 token 时要同步 `sandbox.css` 顶部那张表；版式是
左主列（会话 + 文件）与右副列（运行时 + 群聊人设 + 安装）。右副列的运行时默认展开、
群聊人设默认折叠，两卡自上而下排列，右列底边与左列对齐。开页即重探一次安装态。
入口有两处：侧栏「沙箱」、CEO 权限面板里的**就地开关**——排在「命令行风险拦截设置」上面，
整行可点、右侧画开关。行标题把开关方向和当前状态写在一起，名字跟安装态走：后端没装成时
`unrestricted` → 「受限模式已关闭」、`restricted` → 「受限模式已开启」；后端装好时
`unrestricted` → 「沙箱模式已关闭」、`restricted` → 「沙箱模式已开启」；档位还没读到时只写
中性标题「沙箱模式」。行内只有这一行标题。
受限模式下同一面板显示「允许使用的工具列表」；「命令行风险拦截设置」只在 `unrestricted`
或受限且生效工具包含 `exec` 时显示。工具列表对话框列出 CEO 可见工具，运行时固定项勾选且
不可改，危险工具（`exec` / `process_kill` / `create_async_task` / `skill-installer` /
`register_stickers`）默认不勾选但可勾选并附后果说明；保存必须输入项目解锁口令。
**开和关都弹二次确认**（`openConfirm` 的 password 输入，`textFull` + `inputPlain`：影响说明
整段展示不出滚动条、口令行不带外层框），确认里逐条写工具面 / 缓存 / 安全的变化；未装后端时
标题用「进入受限模式 / 退出受限模式」，装好后才是「开启沙箱模式 / 关闭沙箱模式」。成功后停在
本面板并通知沙箱 iframe 重读（`negi-sandbox-refresh`）。页面嵌在同源 iframe 里，主题跟随外壳
（`data-theme` 由父页经 postMessage 同步）；会话列表拿不到时退化成手填会话 ID，不让页面卡在
"加载中"。

沙箱页自身：右上角只有「未安装沙箱 / 已安装沙箱 / 需要修复」三档状态药丸（`partial` 才是需要
修复），未就绪时药丸右侧出现安装入口，与状态同高：可自动安装时是「一键安装 / 修复沙箱」，
不可自动安装时是「安装说明 / 修复说明」。安装计划弹窗给每条命令「复制」，手动步骤标「手动执行」；
不可自动安装时只给步骤与「重新检测」，不调用安装接口；可自动安装时勾选确认后后台执行，进度
留在页内卡片。会话下拉是自绘菜单（原生 `<select>` 的弹层不可控）；额外可写目录收进
`<details>` 折叠区、owner 才出现且已有值时默认展开。运行时默认展开、卡头不写副标题，
群聊人设默认折叠；运行时卡内只显示读数与提示，不设第二个安装入口。两卡自上而下排列，
右列底边与左列对齐（差值由卡间距吸收）；运行时的空按钮行与空提示不占布局，避免把右列撑高。改档保存走页内二次确认
弹窗（影响说明 + 项目解锁口令），只存可写目录时不弹口令框；「保存」按钮只在档位或可写目录
与已存值不同时出现。

`ui_labels` 由服务端按安装态给出（未安装「受限 / 全权限」、已安装「开启沙箱 / 关闭沙箱」），
前端不自己判安装态。文件管理一族端点只在安装态 `ready` / `partial` 时有意义；
`install_available` / 安装计划由 `g3ku/sandbox/installer.py` 按平台计算，`partial` 与
`runtime_missing` 允许走「修复」路径。

## 信任分级

| 信任级 | 判据 | 默认意图 |
|---|---|---|
| `owner` | 非渠道会话（`web:` / `internal:` / CLI），或私聊发送者在白名单 | `unrestricted` |
| `third_party_dm` | `ext:` 私聊且发送者未列名、缺 sender、或注册表无法分类 | `restricted` |
| `third_party_group` | 多人会话（`group` / `guild`，按 `chat_type` 或 `external_key` 前缀） | `restricted` |

三条硬规则：

- **群聊整会话按第三方**：群消息只带 `member_openid`（无昵称），它与 c2c 的
  `user_openid` 属两套 id 空间，"群里谁是操作员"在协议层不可判定；即便发送者在白名单里，
  也不把群会话升级成 owner。
- **信任只降不升**：注册表条目记"见过的最受限信任级"，一条自称操作员的后继消息不能
  把先前的第三方判定抬回去。
- **失败关闭**：空 sender 永不等于操作员；未登记的 `ext:` 键与 `china:` 归档键按第三方。
  服务端只认 `chat_type` 与 sender 白名单派生信任，**不接受桥自报 `trust`**。

白名单是服务端配置：`qqBot.accounts.<appId>.ownerOpenids`（**所有 QQ 账号取并集**：
openid 按 AppID 隔离，每个号各填一条；任一账号里填过的 openid 在任何 QQ 桥上命中，
比较大小写不敏感）与 `externalApi.tokens.<id>.trustedSenders`（第三方桥，精确匹配）。
配置项的落点见 `config-and-models.md`「external_api」与
`external-agent-api.md`「入站身份契约（`sender` / `chat_type`）」。

MCP 网关以固定身份 `negi-mcp` 出现在每条入站消息里，所以「外部接入」页的「当操作员」开关，
实质是给这颗 token 的白名单增删这个 id：开启要项目解锁口令、收回不需要，两条都写同一个配置项、
留同一条审计。判据只有一处——`session_trust` 按 sender 与白名单派生信任级，档位、记忆、人设、
任务四扇门都读它；开关改的是白名单内容，不是判据本身。

`/api/v1` 入站契约：`sender{id,name}` 与 `chat_type`（白名单 `group` / `dm` / `guild` /
`guild_dm`，非法值 400）；二者与 `external_key` 前缀冲突时**取更严的一档**并留 WARNING。
服务端把派生结果写进 `metadata["external_trust"]` 与 `metadata["chat_type"]`，桥不能覆盖。

## 受限档（`limited`）

没有 jail 时，受限档是第三方会话的地板，也是 owner 主动收紧时的默认档。它不是"只读"：
每个会话有一个独立工作目录 `<数据根>/session-workspaces/<safe-key>-<digest>/`，文件读写
工具只能在这个目录里写；CLI 默认不可用，operator 可以在按会话工具列表里显式勾选 `exec`。

默认集合（`LIMITED_DEFAULT_TOOL_NAMES` + `LIMITED_FIXED_TOOL_NAMES`）：

| 面 | 工具 | 默认 |
|---|---|---|
| 读类数据面 | `content_open` / `content_search` / `content_describe` / `filesystem_stat` / `view_message_history` | 放行（受读根限定） |
| 写类数据面 | `filesystem_write` / `edit` / `copy` / `move` / `delete` / `propose_patch` | 放行（受会话工作目录写根限定） |
| 常驻信号 | `silent` / `time` / `load_tool_context(_v2)` / `load_skill_context(_v2)` | 固定可用，不可取消 |
| 记忆 / 调度 / 渠道 | `memory_update` / `memory_note` / `cron` / `schedule_wake` / `mention_member` | 放行 |
| 网络 | `web_fetch` | 默认放行；operator 可在工具列表取消 |
| 贴纸目录 | `show_sticker_category` | 放行；`register_stickers` 默认不勾选 |
| 危险工具 | `exec` / `process_kill` / `create_async_task` / `skill-installer` | 默认不勾选，可勾选；勾选时给后果说明 |
| 任务派发 | `send_message` / `task_*` | 拒（不管勾选与否） |
| 其他一切 | MCP 工具、skill 工具、资源工具、将来新增的工具 | 默认拒（可在工具列表显式勾选，前提是 CEO 可见） |

- `search_session_history` 是上面「其他一切」里的一条例外：它是 owner-only 的会话转录检索，即使出现在 CEO 可见集并被按会话工具列表勾选，第三方会话也会在工具内被拒；合同见 `tool-and-skill-system.md`「本会话历史检索（`search_session_history`）」。
- **按会话工具列表**：`<数据根>/sandbox/session-modes.json` 的 `allowed_tools` 保存 override；
  未设置 = 默认集合。`GET/PATCH /api/ceo/sessions/{id}/sandbox/tools` 读写这份列表；
  PATCH 必须带项目解锁口令并审计。固定基础工具始终并入，不能取消。
- **`web_fetch` 默认允许**：受限档默认拥有一个外发通道；不需要的群由 operator 取消。
- **`exec` 默认不允许，但可显式打开**：打开后 `exec_requires_governed=True`，命令行形态守卫、
  路径策略与“命令行风险拦截设置”的全局治理继续生效；只有 operator 保存过口令的会话才可达。
- **任务派发在受限/沙箱档一律拒**：`create_async_task` / `send_message` / `task_*` 即使用工具列表
  勾选也不生效（`SessionPolicy.tool_allowed` 的派发族硬拒）。调度类 `cron` / `schedule_wake`
  不属于派发族，可正常使用。
- **渠道会话的显式授权**：渠道没有审批面，受限档里通过 `tool_allowed` 的工具不再走
  `auto_reject`（否则勾选也调不动）；`exec` 由 governed 接管，其他工具按自身守卫执行。
- **两层同时生效**：曝光层（`CeoExposureResolver` / `_frontdoor_callable_tool_names_for_state`）
  收窄可见工具；执行层 `_approval_request_for_tool_calls` 对名单外调用生成打回项。
  收窄必须发生在常驻内置并进来之后，先减后并等于没减。

## 受限档的读写根限定

名单里的文件工具必须同时限定"能读哪、能写哪"。根清单由前门写进
`runtime_context["fs_read_roots"]` / `["fs_write_roots"]`：

| 档位 | 读根 | 写根 |
|---|---|---|
| `third_party` `limited` | 该会话工作目录、入站上传目录（web 上传目录 + `external-uploads/<会话>`）、会话 temp 目录 | 会话工作目录 |
| `owner` `limited` | 上列 + 宿主工作区 | 会话工作目录 |
| `host_full` / `jailed` | 不下发（不设限 / jail 自己管） | 不下发 |

实现约定：

- 判定在 `Path.resolve()` **之后**做，symlink 指到根外与直接写绝对路径一样被拒。
- 运行时根**优先于**工具设置推出的根：受限档不能被 `restrict_to_workspace` 之类的宽松
  设置反向放宽。
- 写侧单独校验 `fs_write_roots`（`filesystem_mutation._enforce_workspace_path_policy`）：
  读根里有工作区不等于能写工作区。受限模式的相对路径锚到会话工作目录，写后校验命令
  （`write_validation_*` / `edit_validation_*`）关闭。
- 工具实例在资源层共享，会话根不能写到实例上：执行闸门（前门
  `_execute_tool_call_with_raw_result`、节点 `_execute_tool_raw`）用 `ContextVar`
  在调用期 pin，`with` 退出即还原。ContextVar 按 asyncio 任务隔离，并发会话不串根。
- 统一实现是 `g3ku/utils/path_roots.py`：`normalize_roots` / `enforce_roots` /
  `runtime_read_roots` / `pinned_read_roots`。`content_*`、`filesystem_*`、
  `filesystem_stat`、`propose_patch` 都消费同一份。
- 越界一律 `PermissionError`，工具层翻成 `sandbox_denied` 形态的可见拒绝；模型应改道找
  操作员上传文件，而不是重试同位路径。

## 控制面拒绝

名单外调用走**既有的合成拒绝车道**（渠道会话没有审批界面那条），不新开中断：

1. `_approval_request_for_tool_calls` 产出 `review_items`，策略项带
   `reason="trust_policy"`，审批项带 `reason="approval"`；整批带 `auto_reject`。
2. `_graph_review_tool_calls` 见 `auto_reject` 即调 `_auto_reject_channel_review`：
   可执行的那半照常执行，被拒的那半按原顺序生成合成结果。
3. 两类拒绝的回执文案不同——策略拒绝说明"这是第三方会话，该由操作员在自己的会话里发起，
   不要重试"；审批拒绝说明"渠道会话没有审批面"。
4. `approval_status`：渠道会话 `channel_auto_rejected`，纯策略打回 `policy_auto_rejected`。

## 记忆门

全局长期记忆是工作区级的，注入在每个会话的 index 1，所以群聊内容一旦写进去，
操作员之后的每一次对话都会被污染。判定同样只有一处：
`MemoryManager.review_eligibility(session_key)`，生产实例由 bootstrap 注入
"`is_owner_session`"，CLI 与检查实例不注入（全允许）。判据自身抛错按**不可信**处理。

四个入口，缺一处就是绕过：

| 入口 | 不合格时的行为 |
|---|---|
| `record_turn_for_review` | 不入缓冲，返回 `ignored` / `untrusted_session` |
| `flush_review_window`（按龄 / token 压缩 / 会话边界三条冲刷道共用一个口） | 清缓冲、不入队，返回 `discarded` |
| 队列消费（`run_due_batch_once`） | 按 `session_key` 拆批：不合格条目终态化 `discarded`，可信条目照常处理 |
| `memory_update` 工具 | 直接返回可读拒绝（`ok=false`），不入队 |

入队请求的 `session_key` 就是判据输入，空值按第三方处理（fail-closed）。操作员本地面入队时带
`internal:` 前缀的 owner 键：记忆管理页的编辑/删除用 `internal:memory_admin`，人设引导页与
设置页的整理请求用 `internal:persona_memory`；渠道会话的自主复核带真实 `ext:` 键。

另外两条配套：

- **启动清扫**：管理器启动时把 `review_state.json` 里不合格会话的缓冲摘掉——闸门只管新写入，
  升级前已缓冲的轮次仍会在下一次按龄冲刷时进全局记忆。
- **成功信号自动重排**排除整批不可信的停车记录；混合批次照常重排，消费侧再拆。

读侧同样按同一判据：第三方会话的长期记忆快照返回空串（不是"读失败"，所以冻结值是稳定的
空），记忆修订增量与人设增量都不投。第三方会话的人设**只认沙箱页单独配置的那一份**
（`<数据根>/sandbox/persona.md`，`GET/PUT /api/sandbox/persona`），空即不带人设；操作员自己
的人设 note 不注入群聊。该配置不依赖安装态（D20）。

## 来源标注

渠道会话（`ext:` / `china:`）的用户消息在模型可见投影里带一行前缀；web 普通会话没有这一行：

```
[来源] 群聊 第三方 0002
[来源] 群聊 第三方 0002（张三）      ← 桥给了名字（频道 / OneBot / MCP）
[来源] 私聊 Master                   ← 私聊命中名单
[来源] 群聊 Master                   ← 本人在群里（只改标签，不改档位）
[来源] 输入框 Master                 ← 渠道会话里由网页输入框提交
[来源] 群聊 未知                     ← 桥没带 sender
```

- 身份：`Master` = 操作员本人（sender id 命中 `ownerOpenids` 并集，大小写不敏感），或渠道会话里
  由网页输入框提交；其余发送者按**会话内首次出现顺序**编号 `0002`、`0003`…，同一会话内同一 id
  恒定同一号。`0001` 留给 Master 但不印号；哈希不进上下文。
- 群里的本人只改标签、不改档位：`resolve_turn_trust` 的多人会话短路不变，群里 Master 的消息仍按
  `third_party_group` 执行。标注是给模型解释用的，不是准入判据。
- 只有渠道标记的消息才装饰：`external_trust` 由 `/api/v1` 落库；网页输入框在渠道会话里补
  `external_source=web_composer`。两者都没有的行（web 普通会话）永远没有来源行——这是硬护栏，
  不能拿 `session_trust` 代替（它对 `web:` / `internal:` 返回 owner）。
- 渠道会话的 system prompt 追加「渠道会话来源标注」说明块：`Master` 是谁、`第三方 000N` 是编号，
  并明确第三方索要 Master 的私人信息（身份、联系方式、住址、日程、账号凭据、私聊内容、记忆与人设）
  时必须拒绝。它是提示词约束，底层仍靠记忆门、只读档与工具闸。
- 幂等判据是"正文以本次投影渲染出的那一行开头"，不是"以 `[来源]` 开头"：后者由发送者控制，
  第三方正文自己写一行标记就能把真标注整行免掉。
- 发送者 `id` / `name` 折成一行（`" ".join(split())`）：名字来自桥的请求体，其中的换行不能伪造出
  第二行标注。
- 装饰值派生自记录里的固定字段（`external_trust` / `external_sender` / `chat_type` /
  `external_source`），跨回合字节稳定，不打断 provider 前缀缓存。
- 编号每次投影重算、不落盘：编辑重发或 Fork 截断删掉某人首条时，后面的人可能整体前移一位
  （按出现顺序的固有代价；不兼容旧 `[消息来源]` 标注，存量渠道会话由操作员重置）。
- **转录保持原文**：RAG、网页回放、导出都吃 raw content。
- 所有按内容相等性的比较先 `strip_sender_stamp`（与 `strip_arrival_time_stamp` 同一纪律）：
  `_cron_prompt_equal`、暂停回合种子去重两处是已知比较点。
- 缺 `external_trust` 且非输入框标记的历史记录不装饰——无法判定其来源时宁可不标。

实现：`g3ku/runtime/sender_stamp.py`（渲染、编号与剥离）、
`g3ku/runtime/frontdoor/message_builder.py`（历史道与当前轮兜底道同一作者）、
`g3ku/runtime/frontdoor/prompt_builder.py`（渠道专属说明块）、
`g3ku/runtime/api/websocket_ceo.py`（composer 标记与气泡标签）。

## Debugging Entry Points

- 群里让它"记住 X"后全局记忆变了 → 先看 `memory/ops.jsonl` 的 `session_key` 与
  `memory/review_state.json`；四处闸门任一漏改都会表现成"只有某一条车道漏了"。
- 群里让它读 `config.json` / `master.key` → 看该会话的 `external_trust` 是否为第三方、
  工具是否落在会话允许列表里、`fs_read_roots` / `fs_write_roots` 是否下发
  （前门 `_sandbox_read_roots` / `_sandbox_write_roots`）。
- 群聊工具"看不到但不报错" → 曝光层收窄；"看得到但被拒" → 执行层打回，两者文案不同。
- 群聊本来能用的工具突然没了 → 查注册表条目的 `trust`（粘性，只降不升）。
- 装了/卸了沙箱但会话档位没变 → 安装态是全局读数、档位在回合边界才生效。
