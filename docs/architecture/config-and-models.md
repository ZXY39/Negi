# Negi 配置与模型系统说明

本文档解释项目配置如何加载、模型如何绑定到运行时、哪些数据写在 `.g3ku/config.json`，哪些数据在 `llm-config` 存储里。

## 1. 配置入口

项目配置的统一入口是：

- `g3ku/config/loader.py`
- `g3ku/config/schema.py`

其中：

- `loader.py` 负责读取、迁移、保存、强校验
- `schema.py` 定义完整的 Pydantic 配置模型

当前项目明确要求配置从项目本地路径读取：

- `.g3ku/config.json`

该路径始终相对安装根（进程 cwd），与数据根无关——选过自定义数据目录后配置仍在代码检出目录里。

## 2. 配置模型的几个核心部分

### `agents`

定义：

- 默认 workspace（`agents.defaults.workspace`）：允许留相对值（出厂默认 `.`），但首次读取就按当时进程目录解析成绝对并缓存，之后即使进程切换工作目录（`g3ku/web/launcher.py` 的启动 chdir 就是这种情况）锚也不会移动；缓存键是原始字符串，改了配置值会重新解析。它同时是模型相对路径的锚，落点规则见 `tool-and-skill-system.md`「Model Path Anchoring Contract」
- runtime 模式
- 温度、max tokens、memory window
- role iterations / concurrency
- multi-agent 配置

### `models`

定义：

- `catalog`
  管理模型条目
- `roles`
  把模型路由到 `ceo / execution / inspection / memory`。每一项要么是一个绑定 key（旧写法，等价于 `{"type":"model"}`），要么是一个 route entry：`{"type":"model","modelKey":...}` 或 `{"type":"load_balance","groupKey":...}`
- `loadBalanceGroups`
  可选节。组名 → `{enabled, maxRetryRounds, modelKeys}`：组内成员平级，运行时按综合负载选一个并把节点粘滞绑上去；`maxRetryRounds` 是该组内每个成员的完整 key pass 预算，非负整数（不填按 1，0 与 1 等价），负数是配置错误而不是需要夹断的输入

`catalog[]` 条目另有 `quotaPoolKey`：operator 显式声明「这几条 binding 共享同一个上游配额账户」。不填时运行时只按解析到的 endpoint + API key 指纹自动合并；不按 provider 名称猜测共享。这个值同时出现在路由的成员视图（`RouteMemberView.quota_pool_key`）上，与桶指纹同源——成员视图读不到它时按「身份未声明」处理，而不是按「不共享」处理。

### `providers`

保存 provider 级基础信息，例如：

- `api_key`
- `api_base`
- `extra_headers`

### `web`

定义 Web bind host / port，默认 `127.0.0.1:18790`。

出厂默认只绑回环，因为**管理面这套接口没有鉴权依赖**（`main/api` 全局没有 `Depends`，唯一那道门是首屏锁定中间件 `web/main.py`），而它能删任务、改模型配置与密钥、写 exec 命令白名单。把 host 显式写成 `0.0.0.0` 等于把这些动作开放给整个网段——容器车道就是这么显式传的（`docker/web-entrypoint.sh` 带 `--host 0.0.0.0`），那是"外面另有边界"的部署形态，不是本机默认。

取值优先级：`negi web --host` > `config.json` 的 `web.host` > schema 默认；`start-negi.cmd` / `start-negi.ps1` / `start-negi.sh` 自己显式传 `127.0.0.1`，`g3ku/web/launcher.py` 在前两者都空时兜底回环。已经写在 `config.json` 里的值优先于默认，所以一台盘上已经是 `0.0.0.0` 的设备不会被默认改动收回去——那要操作员自己改那一行，或按上面的边界自己承担。

### `resources`

定义 skills / tools 资源目录与 reload 策略。

### `main_runtime`

定义任务运行时存储与调度参数。其中 `main_runtime.duplicate_precheck.llm_review_enabled`（默认 `true`）控制 `create_async_task` 重复预检的第二层语义审查：关闭后只保留确定性精确匹配层，模糊重复与 `reject_use_append_notice` 识别随之消失，预检对放行结果 fail-open。字段契约与拒绝语义详见 `tool-and-skill-system.md`「fixed builtin tools」。

`main_runtime.node_tool_timeout_ceiling_seconds`（默认 900）是节点道工具超时的机器上限；`main_runtime.max_task_nodes`（默认 200，验收节点计入额度）是单任务节点总数闸门。`main_runtime.node_watchdog_interval_seconds`（默认 30）与 `node_watchdog_stall_seconds`（默认 600）控制 P6 节点看门狗：扫描间隔与「无新事件」判停窗口（行为契约见 `main-task-runtime.md`「看门狗」）。

`main_runtime.node_dispatch_concurrency.execution` / `.inspection`（默认 8 / 4）是**节点回合闸共享池的地板**——池地板＝两者之和（默认 12），是空闲排空后的复位值，既不是任一角色的上限也不是下限保证。容量是一份共享池，角色只握保留格 `min(自己的地板, ceil(池地板/4))`：保留格只在该角色**有等待者**时挡住别的角色借走，没人等就可以被借走（各角色各自封顶时实盘 10% 的采样拍出现"一侧空 7 格、另一侧排队 11 个"）。池位由监控按**节拍**（`_ENTRY_BEAT_SECONDS`，实测一次模型调用 p50 37 s）发布的余量目标决定：本地积压轴（事件循环 lag、写入队列、SQLite 读写、回环连接条数）越过 warn 线只保持、越过 critical 线每节拍退一格，任何退让都止于地板（能一次踩到 0 的只有磁盘紧急硬闸）；机器内存余量与 1.5×池地板的硬顶只限制它往上长；**上游 429 限流不参与容量判断**，它的收口处是模型链自己的退避与换槽（见「角色路由：有序 fallback 与负载均衡组」）。界面上的「限」读的是活池位（`dispatch_pool` 与 `dispatch_limits`），不是这两个配置数。调数经一次配置刷新即打到活着的闸，无需重启：任一数被调小 ⇒ 池地板与保留格当场重算、当前池位钳到新地板；两数都不变 ⇒ 保留已抬到的位置；闸口排空后自动回到地板。worker 模式同样生效。完整判据与读数口径见 `runtime-overview.md`「节点回合闸」。

`main_runtime.disk_guard` 子节控制磁盘写保护与治理（行为契约见 `runtime-overview.md`「磁盘写保护与治理」）：`write_guard_enabled`（默认 `true`；关闭后写异常恢复原样上抛、不做应急预算预检）、`emergency_min_bytes`（默认 300MB）与 `emergency_min_ratio`（默认 0.01，紧急线取两者较大值）、`usage_ttl_seconds`（水位探测缓存 TTL，默认 5s）、`artifact_gzip_threshold_bytes`（默认 1MiB；`<=0` 关闭 artifact gzip 压缩）、`terminal_cleanup_enabled`（默认 `true`；关闭后任务终态不自动清理确定用不到的数据）、`terminal_temp_dir_cleanup_enabled`（默认 `false`；开启后终态硬删 `temp/tasks/<id>`）、`auto_pause_enabled`（默认 `true`；关闭后紧急态只检测告警不自动暂停任务）、`emergency_streak_samples` / `emergency_recovery_samples`（进入/解除紧急态的连续采样拍数，默认 3/5）、`alert_on_disk_emergency`（默认 `true`）、`detail_retention_days`（终态任务大行保留天数，默认 `0`=停用裁剪——任务明细与任务同生命周期，仅随手动删除清除；配置 `>0` 恢复按天裁剪）。磁盘治理没有清理线/自动删任务相关配置；删除台账保留期（7 天）是 `runtime_service` 模块常量，不进配置。`G3KU_*` 同名环境变量仅作测试与应急覆盖，配置源真值以本子节为准。

### `external_api`

定义 External Agent API（`/api/v1`）的启用与桥接凭据：`enabled`（默认关）、`tokens`（bridge_id → `{token, label, enabled, crossSession}`）、`eventBufferSize`。token 密文走 bootstrap secret overlay 三件套（保存时剥离进覆盖层、落盘配置只留占位、解锁时回填）。作用域只按号裁决：`tokens[].crossSession` 是布尔、默认放开，落盘时总写出来；写判据按 key 存在与否、不按真值，否则 `false` 会被当成"没填"。存量配置里那个已删除的全局档 `crossSessionEnabled` 在模型加载时折进每个没有单独表态的 token 然后丢弃（`ExternalApiConfig._fold_legacy_global_scope`）——不折的话，当年关掉过全局的部署会在升级后被静默放开成"每个桥都能枚举并投递全部会话"。字段语义与鉴权契约详见 `external-agent-api.md`「启用与鉴权」。

### `cron`

定义 cron 调度器的投递看门狗预算：`dispatchTimeoutSeconds`（默认 1800，单次 job dispatch 的等待上限，超时会 dump 挂起任务的 await 链、取消 dispatch 并把该次运行收尾为 `timeout`；`<=0` 关闭看门狗回到无限等待）与 `dispatchCancelGraceSeconds`（默认 10，取消后等待任务展开的宽限，超时视为抗取消任务并脱离调度器放弃）。默认预算刻意大于最长合理回合（provider 单次尝试上限 + 退避重试轮），只兜底真正楔死的 dispatch。该节属于加载器「显式字段校验」的豁免前缀：存量 `config.json` 不写 `cron` 节也能启动，取值回落到 schema 默认。行为契约详见 `heartbeat-system.md`「Cron Reminder Contract」。

### `stt`

定义本机语音识别（默认开启，关掉它是操作员说"别碰我这条车道"）：`enabled`、`model`（`tiny`/`base`/`small`）、`language`、`threads`、`maxAudioSeconds`、`minRmsDbfs`、`timeoutSeconds`、`simplifyChinese`、`modelDir`（默认 `.g3ku/stt`，跟随数据根）、`binaryPath`、以及分发四件 `binaryReleaseTag` / `binarySha256` / `binaryDownloadBaseUrl` / `modelDownloadBaseUrl`。没有密钥字段，因此不参与 secret overlay 的抽取/剥离三件套。

默认开的是**许可**，不是那 157 MB 的下载：二进制与模型仍要显式取得（`negi stt prepare`，或网页首次点麦克风时按需下载），所以一台没下过东西的机器上 `ready` 仍是 false，引擎不会自己去联网。反过来，已经保存过配置的旧设备里 `enabled: false` 是写死在 `config.json` 里的，不会随默认值翻转——那是它自己的显式选择。

`stt` 的写侧两个判断（在豁免前缀里、且每个字段都必须进白名单，否则 `negi stt prepare --enable` 落不了盘）见「写侧：`save_config` 整写与字段白名单」。引擎形态、就绪判定与默认值的实测依据详见 `speech-to-text.md`。

### `stickers`

表情包发送预算：`maxPerReply`（默认 1；0 = 关闭发送但标记仍会被剥掉、不会漏进正文；上限 4，与单条渠道消息的附件预算 `MAX_CHANNEL_ATTACHMENTS` 对齐）。属于「调过才落盘」的可选旋钮，同时在豁免前缀里——存量 `config.json` 没有这一节必须还能加载。清单本身不在这里配置：它由 `<workspace>/stickers/` 目录扫描得到。这两个值都能在网页的 表情包 页直接改（`PUT /api/resources/stickers/settings`，落盘后走同一条 runtime refresh）；页面契约见 `web-and-admin.md`「Sticker Pack Page And Admin Contract」。

## 3. 配置加载时做了什么

`load_config()` 不是简单读 JSON，它会做很多迁移和约束检查：

- legacy 字段迁移
- gateway / channels / old tools config 清理
- role iteration / concurrency 补默认值
- LLM 相关旧配置迁移
- secret overlay 应用
- 运行时字段显式性校验

这意味着：

- 启动时报配置错误，很多不是 JSON 语法错，而是 schema contract 变更
- 不要手写“看起来像旧版”的配置字段，loader 会直接拒绝

### 写侧：`save_config` 整写与字段白名单

`save_config()` 把 `config.json` **整份重写**（先写同目录临时文件再 `os.replace`），内容来自 `_runtime_config_payload(cfg)` 这一份显式白名单，不与磁盘上的旧文件合并。因此一条硬规则：**运行期会读的字段必须出现在白名单里，否则任何一次保存都会把它抹回 schema 默认**——操作员调过、界面改过、`G3KU_*` 覆盖过都算写过，而下一次保存就不见了。

白名单对可选字段按「调过才落盘」写：与默认值相同的字段不落盘（不给操作员文件加噪声，存量文件也不会因此长出键），只有被改动的字段进文件，读侧由 pydantic 补回其余默认值。当前按该形状落盘的有 `agents.nodeParallelism`、`agents.toolDefaultTimeoutSeconds`、`mainRuntime.diskGuard`、`mainRuntime.duplicatePrecheck`、`mainRuntime.eventHistory`、`updateCheck`、非空时才落盘的 `models.loadBalanceGroups`，以及 `models.catalog[].reasoningContextEnabled`（这颗开关默认开，只有被操作员关掉的那一行写 `false`——它不落盘就等于下次保存把网关会 400 的那一行重新打开）。

`agents.nodeParallelism` 里的机器压力读数分档是 `safe < warn < critical`，三条轴（cpu、memory、disk busy）同形，`tool_pressure_monitor` 按这个顺序把样本分成 `normal / warn / critical`，并要求连续 `safe` 采样才算机器侧恢复。写反的档位不会报错，只会静默失效：`machine_safe` 判的是 `读数 <= safe`，一旦 `safe` 高于 `warn`/`critical`，warn 与 critical 区间照样读成"已恢复"，机器侧那半条降档闸门就永远不落地。默认值由 `tests/resources/test_task_web_worker_runtime.py` 的分档用例钉住，改这三组数字要一起过它。

「运行时字段显式性校验」要求白名单里每个叶子都在 `config.json` 出现，缺一个就拒绝加载。豁免前缀（`providers`、`mainRuntime`、`externalApi`、`qqBot`、`cron`、`stt`、`stickers`、`agents.nodeParallelism`、`agents.toolDefaultTimeoutSeconds`、`updateCheck`、`models.loadBalanceGroups`）承担两类情况：存量文件本来没有这些节，以及取值可能来自 `G3KU_*` 环境变量而不是配置文件——被豁免的节没写就是不用，回落到 schema 默认。新增字段时两个判断都要重做：进不进白名单（决定它会不会被保存抹掉），要不要进豁免前缀（决定老文件还能不能加载）。摘掉一个配置段时同样有两步：从 schema 与白名单里删掉它，并在 `_migrate_config()` 里 pop 掉它的磁盘拼写——根配置是 `extra="forbid"`，存量 config.json 里残留的那一节会让 `load_config()` 当场拒绝加载。

## 4. 配置热刷新

运行时读取不总是直接 `load_config()`，而是走 `g3ku/config/live_runtime.py` 的 `get_runtime_config()`：

- 按 `.g3ku/config.json` 的 `mtime` 检测变更
- 维护 revision
- 刷新失败时保留 last good config

这对维护者很重要，因为它解释了两个现象：

- 改配置后为什么有时不必整进程重启
- 配置写坏时为什么服务可能还暂时“看起来能跑”

管理面保存模型配置时，还要区分两层刷新：

1. Web 进程自己的 runtime refresh
2. Web 托管 worker 的 runtime refresh

当前行为是：

- 保存类接口先写盘
- 写盘成功后立即返回成功响应
- worker 刷新改为异步命令，通过 `task_commands` 中的 `refresh_runtime_config` 记录确认是否真正应用

因此，`200 OK` 只表示配置已经保存成功；它不等价于“worker 已经确认加载新配置”。

刷新还要区分“模型路由”与“记忆运行时”两类影响面，由 `refresh_web_agent_runtime(...)` / `refresh_loop_runtime_config(...)` 的 `force_memory_sync` 控制：

- `force_memory_sync` 默认 `False`：记忆运行时同步走 `memory_runtime` 资源指纹门控，指纹未变就不重置。模型路由、CEO 链调整、回合中刷新（`provider_retry_invalidation`、`_resolve_ceo_model_refs`）都属此类。
- 只有真正改写指纹树之外的记忆相关设置时才显式传 `True`：`run_llm_migration`、`model_config.migrate_legacy`，以及 `update_llm_config` 命中的是绑定引用的记录时。
- 原因：强制重置会在回合进行中重置 memory manager 与 commit service，干扰在途会话的记忆读写；因此纯模型路由/链调整不应强制重置。

### 模型链变更何时作用于在途回合

- 配置刷新不会把一个“已经发出去的单次 provider 请求”中途热切换到新模型；切换只作用于边界处重建的下一个请求。
- CEO/frontdoor 在每次 `call_model` 迭代边界（含 provider-failure retry / empty-response retry 边界）对比 runtime revision：revision 变化时重新解析当前角色模型链并写回轮状态，下一次 provider 请求、上下文窗口估算与绑定模型链的能力判定（如 `content_open` 的多模态闸门）都跟随新链。轮状态用 `model_refs_revision` 记录解析时的 revision，作为下一次边界对比的基线。
- 因此 `model_config set_scope_chain` 之后，同一轮的下一次模型调用即用新链；在同一步把切链与被门控的工具调用作为并行工具调用发出时，闸门仍按切换前的链判定。
- Main runtime 节点的模型链在任务运行时 chat 后端的每个链轮边界活解析（纯 direct 链由 `model_refs_resolver` 读取当前角色链；链上含组时该活解析只以 route plan 为真源，候选展开不参与链替换，一槽一位的约束见 `runtime-overview.md`「Chat provider 超时与重试边界」），路由/绑定变更在下一个链轮即生效，不必等下一个回合；可重试失败退避等待结束后同样先重新解析链再继续重试。**链在重试途中被改写不会中止回合**：退避边界发现 runtime revision 变化且能解析出与在用链不同的新链时，重试循环丢弃旧链的已试集合、刷新 revision 基线并从新链链首重新评估，成功即正常返回；链未真正变化时只刷新基线，继续原有重试账本，不在同一 revision 上空转。按链首重启意味着新链里被跳过的模型重新获得机会。`DEFAULT_MAX_CHAIN_CHANGE_RESTARTS`（`g3ku/providers/fallback.py`）跨重启累计，只兜底"链反复变化且始终不成功"的病态抖动，达到上限后落既有链耗尽终态。CEO/frontdoor 与二者之上还有各自的回合级守卫：`react_loop` 在链耗尽错误处按 `provider_retry_invalidation` 重建回合，frontdoor 在 provider 失败/空响应边界重新解析角色链，二者都只在 `ensure_runtime_config_current` 报告配置确有前进时触发。memory queue 内部 agent 看的不是 CEO/node 的 provider retry，而是 memory 自己的同批次 validation/repair 重试点，普通 review window 不经过单独的 `assess -> apply` 交接。

一次模型调用（一个 `chat` 调用）内部还有比"链轮"更细的一格，它承接组定义那半张配置面：

- **每一轮重试之前**都对比 runtime revision，并把活配置重建出的槽位表无条件采纳为在用槽位表——组的每成员重试轮预算（`maxRetryRounds`）、成员名册、成员的声明窗口都跟着改值。因此把预算从 999 调成 2 时，正在重试的那颗成员就在下一轮让位；调大时同一条成员当场获得多出来的轮数；重试途中加进组的成员会出现在这一发的换人名册里，而不是等下一发调用才发现它。
- 采纳槽位与"重启整条链"是两件事，判据不同：换槽位表不动已试集合，也不回到链首；只有链的**成员列表**真的变了才清已试集合并从链首重新评估。把两者捆在一起会让纯数值改动（成员列表不变）被整份丢弃。
- revision 对比必须排在预算比较**之前**。嵌在"本轮失败且还没跑满预算"里面时，旧预算=1 的那扇门根本进不去，调大追不上；旧预算=999 时会按旧值一路烧完才让位——这两个方向都是操作员报"改了不生效"的实际形态。
- 残留的边界是一次 attempt 之内：provider 目标、该模型的 key 轮序、attempt 超时在这些值于槽位入口取一次之后不重读，单次已发出的 HTTP 请求永不热切。操作员可见的最长滞后因此是"当前这一轮"，不是"这一发调用"，更不是"这个回合"。

节点侧的路由结构有两个落地点：worker **启动构造**与管理面的**配置刷新**。两处都重建 `execution` / `inspection` 的 route plan，并且都在同一步把同一份组定义连同 revision 交给负载均衡器（`_apply_model_route_plans`；契约见 `runtime-overview.md`「节点模型路由与准入绑定」）。这个"同一步"是硬约束：准入层对链上每个 `load_balance` entry 都要能在 balancer 里查到那个组，查不到就按 `unknown_group` 跳过整条链，节点永远拿不到授予、队列冻在 waiting。新起的 worker 不会自己跑配置刷新——只有管理面保存下发的 `refresh_runtime_config` 命令才会触发它——所以注册组必须落在启动路径上，链上带组的配置才能在每次重启后继续工作。revision 只作观测，不参与重绑判定：刷新只解绑「绑定成员已被移出组」的那些节点，其余绑定与最近的请求速率、429 惩罚一律保留——那两项描述的是上游，与本地配置有没有被改过无关。因此一次与路由无关的保存（改 prompt、改渠道设置）不会打断任何节点的缓存粘性。

维护上把这理解成“迭代/重试边界上的重建”，而不是“请求中途热切模型”。如果用户反馈“改完模型链后旧模型还在用”，重点检查：

1. 对应进程是否真的执行到了 runtime refresh（日志 `Loop runtime config refreshed`）
2. 问题是否发生在单次 still-in-flight 的 provider request 内（该请求不可热切），还是跨过链轮/轮次边界后仍未换链；组的每成员预算与成员名册按上一段在每一轮边界重读，`Retryable model failure … round x/N` 的 N 跟着新值走才算落地
3. 当前运行路径是 CEO/frontdoor、main runtime worker/node，还是 memory queue 内部 worker
4. 节点全停在模型侧队列（`node_queue_waiting_count>0` 而 `node_queue_running_count=0`）且心跳里 `model_route_groups` 为空时，问题在本地落地而不是上游：启动路径没把组注册进 balancer，`Model route selected` 一条都不会出现。闸收紧期间的等待不看这条数——`node_queue_waiting` 量的是过闸之后等模型 permit，闸口排队的读数是 `entry_gate_queued_total`（契约见 `runtime-overview.md`「节点回合闸（执行器存在的成本也要闸）」）

## 5. 模型系统不是只靠 `config.json`

这是新人最容易误解的一点。

Negi 的模型系统分两层：

1. 项目配置中的模型绑定与角色路由
   `models.catalog`
   `models.roles`

2. `llm-config` 子系统里的 provider config record
   位于 `.g3ku/llm-config/`

`config.json` 更像“项目如何引用模型”，而不是所有模型秘密和 provider 配置的最终存储地。

## 6. `llm_config` 子系统

关键入口是：

- `g3ku/llm_config/facade.py`

它负责：

- provider config record 的增删改查
- 绑定模型 key 到 config record
- 导出 runtime target
- 把 secrets 存进安全 overlay，而不是明文长期放在 record 中
- 为管理面「添加模型」流程提供 draft 校验、连接探测、最大并发探测与供应商模型目录拉取。其中**连接探测与最大并发探测只由显式的探测动作发起**：创建、修改、改名、开关、删除这些写路径一律不发真实请求（管理面契约详见 `web-and-admin.md`「Model Config Page And Admin Contract」）

facade 的存活期是**每 workspace 一个实例**，热路径一律经 `get_llm_config_facade(workspace)` 取用而不是就地构造：构造它会连带构造配置仓储，而仓储构造要无条件 `mkdir` records 目录并探测 `index.json` 是否存在。记录内容不做缓存——每次 `get()`/`list_summaries()` 都从磁盘读并重新校验，所以管理面改完配置运行侧立刻可见；被固定在构造时刻的只有 legacy master key 这一项，因此安全域必须在首次模型解析之前建好。

协议（`protocol_adapter`）是记录级派生字段：它由 `provider_id` 命中的 provider 模板唯一决定，draft 里同名参数不参与解析，因此切换协议等于换模板而不是写一个独立字段；归一化后的值随 runtime target 导出，由 `g3ku/providers/provider_factory.py` 决定构建 Chat Completions 还是 Responses provider。模板同时决定 `parameters` 的字段集合与 `reasoning_effort` 白名单，两者必须保持一致，否则管理面会给出保存得了但校验不过的字段。

Responses 协议的请求体只带各家 `/responses` 共同支持的字段：`text.*` 这类 OpenAI 扩展会被代理到 Chat Completions 后端的供应商整单拒绝，`instructions` 同样会被判成 `inference request is invalid`，因此开头的系统提示只以 `input` 首位的 `[SYSTEM]…[END SYSTEM]` user 块送达，运行时注入的中段块以 user 角色留在原位（角色与位置合同见 `context-and-cache-troubleshooting.md`「线体角色合同」），深度思考走 `reasoning.effort`。探测体的输出预算取各家共同接受的最小值而不是最小值以下的数：带下限的 `/responses` 端点会把低于其下限的 `max_output_tokens` 整单打回（实测报 `field max_output_tokens invalid, should be in [16, 393216]`），而探测只读 HTTP 状态行，用一个能被拒的预算做探测等于把健康的模型判成不可用；真实发送体的 `max_output_tokens` 走 `max(1, 配置的 max_tokens)`，所以把输出预算配到该下限以下时，报错仍落在第一个真实回合而不是保存那一步。连接探测发的是同一个请求形状——一条 ping 加一个走各自协议 normalizer 的占位函数工具——并且模型目录可读不算通过：目录之后还要过一次真实形状的推理，被拒时把上游原文带回消息，字段级不兼容因此在「测试连接」这一步就暴露，而不是等第一个真实回合。保存本身不探测，所以没测过连接、甚至字段会被上游整单拒掉的配置照样能落盘：探测是显式发起的检查，不是写入的前置条件。管理面「完整上下文」侧的预览请求体与真实发送体保持同形状，否则预览给出的字段清单不可信。

## 7. 运行时是如何拿到模型的

典型路径如下：

1. `Config.resolve_role_model_key("ceo")`
2. `Config.get_scope_model_target(...)`
3. `bootstrap_factory.make_provider(...)`
4. `g3ku.providers.chatmodels.build_chat_model(...)`

若某模型条目绑定了 `llm_config_id`，则：

- `Config` 会借助 `LLMConfigFacade.get_binding(...)` 解析真实 provider/model

这意味着：

- 运行时看的是“role -> model key -> binding -> provider target”
- 不是简单的“role 直接写死 provider:model”

绑定 key 是稳定主键：它唯一标识 `models.catalog[]` 条目，同时被 `models.roles.*` 和 `agents.multi_agent.orchestrator_model_key` 引用。管理面创建 binding 时以记录的 `default_model` 为基底自动生成 key，遇到同名模型时追加数字后缀去重，因此 key 不等于模型名，不同供应商可以添加同名模型。展示标题的优先级是绑定级 `name` > 记录的 `default_model` > `key`：`name` 是 `models.catalog[]` 条目的绑定层字段，空值为「未命名」，展示回退到 `default_model`，写入空 `name`/删除 `name` 即回到回退展示而不改写 key；非空 `name` 在创建/编辑 binding 时做大小写不敏感的全局去重（排除自身），`/api/models` 与 `/api/llm/bindings` 两个视图都读写该字段。编辑 `default_model` 或 `name` 都会更新展示标题而不改写 key；命名与展示职责详见 `web-and-admin.md`「Model Config Page And Admin Contract」。

### 角色路由：有序 fallback 与负载均衡组

一条角色链是**有序 route entry 列表**，链上顺序就是 fallback 优先级。CEO/frontdoor 的 `model_refs` 不被过滤也不按 provider 模板或协议能力重排；只有链首失败后才前进到下一个 entry（重试与轮换预算见 `runtime-overview.md`「Chat provider 超时与重试边界」）。

`execution` / `inspection` 额外允许把一跳写成负载均衡组（`loadBalanceGroups` 引用）。语义差别是维护者最容易读错的地方：

- 组内成员**平级**，`modelKeys` 的书写顺序只用于展示与稳定序列化，不参与选择；链上 entry 的顺序才参与。
- `models.roles.*` 与所有「候选展开」出口（`get_role_model_keys`、`Config.get_scope_model_chain`、管理面 `roles` 字段、`facade.get_routes`）都是**候选视图**：组被摊成成员列表。任何把「候选数组第一项」当成实际执行模型的代码都只在 legacy 纯 direct 链上成立；含组时真正的成员由准入层的绑定决定（契约见 `runtime-overview.md`「节点模型路由与准入绑定」）。
- `route_entries` 才是有序结构。管理面与 `model_config` 工具都读写它，`roles` 保留旧形状给存量客户端。
- 会话 agent 改链走 `model_config`：`get_scope_chain` 同时给出候选展开与有序 `route_entries`（外加该链的轮数、并发与保守收图交集），`set_scope_chain` 接受 `route_entries` 或扁平 `model_keys`。扁平提交是**整条替换**，所以含组的链必须回写有序形状，否则组槽位就在这一跳消失；组本身的增删改仍只在管理面，工具引用不存在的组会读到 `Unknown load balance group`。两条写车道命中同一个 `update_scope_route` 与同一次 runtime refresh，route plan、balancer 组状态、准入这一串级联不在工具里复刻（生效时机见「模型链变更何时作用于在途回合」）。
- 落盘形状：一条链全是 direct 时继续写字符串数组（存量 `config.json` 零 diff），一旦出现组 entry 才整条改写成对象数组。读侧两种形状都吃。
- `ceo` / `memory` 出现 `load_balance` 会被直接拒绝（负载均衡组当前仅支持 execution/inspection）。记忆车道有固定单并发与 chat capability 契约，CEO 有会话固定模型与会话链约束，都不能被组语义覆盖；会话链（`model_selection.model_keys`）同样是扁平 key 列表。
- `mainRuntime.modelRouteLoadBalanceEnabled = false` 是回滚闸门：含组的链按配置顺序摊平成 direct 候选，准入与发送侧一起回到有序链行为。
- 组是**全局资源，不属于任何一条链**：`_prepare_scope_route_update` 在落链之前先落整份 `models.loadBalanceGroups`，所以一次 scope 保存可以只带 `model_keys` 加一份组集合（链里还没有组也行）——管理面的组配置列因此允许「先建组、之后再拖进链」，而组弹窗的「确定」本身就是这一处配置的落盘点（写车道与显示判据见 `web-and-admin.md`「Model Config Page And Admin Contract」）。也正因每个 scope 的保存都整份替换该字典，客户端必须交**完整**组集合，交子集会把没在这条链上用到的组删掉。成员为空的组不落盘（`modelKeys` 必填），因此链指向一个空组时会得到可读的 `Unknown load balance group`，而不是静默少一跳。
- 组名是引用位的一部分：改名要同时重写所有链上的 `group:<key>` 记号（管理面在前端一次改完），删除组要连带摘掉引用它的链位。组名撞模型 key 由 schema 直接拒。

两条与序列化器绑定的维护陷阱：

- `models.loadBalanceGroups` 是「调过才落盘」的可选段，也在显式字段校验的豁免前缀里；不豁免的话，任何一份没有该节的存量 `config.json` 都会在下次加载时被要求填写组字段。判据见「写侧：`save_config` 整写与字段白名单」。
- 写链必须走 `Config.set_role_model_keys` / `set_role_model_routes`。字段类型是 `ModelRouteEntry` 之后，就地 `append` 裸字符串不会被 pydantic 拦下（未开 `validate_assignment`），但会让改名、删除这类按 key 比较的写路径静默失配。改名要同时命中链上的 direct entry 和组内成员（`rename_model_key_in_routing`）；把模型从组里删到空是报错而不是顺手删组。

- provider 的 `supports_prompt_caching` 不参与模型选择。它描述的是这条车道转不转发 `prompt_cache_key` 这个请求字段，而不是这个模型吃不吃得到缓存——命中来自网关侧自动前缀缓存，两家车道都会回报 `cached_tokens`（取证口径见 `context-and-cache-troubleshooting.md`「Family 与 key 合同」）。把它当成路由门控会把不具备该字段的模型整段移出链，连带取消它们的容灾资格。
- 「面板显示的模型不像链首」按两个字段判读：preflight diagnostics 的 `resolved_model_key` 是本轮生效的绑定 key，`provider_model` 是 provider 侧模型名。多条绑定可以共用同一个 `provider_model`，只有绑定 key 能区分它们。含组时 `resolved_model_key` 就是被选中的那个组成员。

### 会话级模型链与固定模型

Leader（CEO/frontdoor）解析本轮模型引用时按三档取值，顺序固定：会话固定模型 > 会话链 > `models.roles.ceo`。设置住在会话元数据的 `model_selection`：

- `{"mode": "model", "model_key": "..."}`：本轮 `model_refs` 是该固定模型（单元素），不走任何链。
- `{"mode": "chain", "model_keys": ["b", "a"]}`：本轮用这条**会话链**，顺序即 fallback 优先级，只作用于该会话。
- `model_keys` 为空或整键缺席：走全局 `models.roles.ceo`。会话从没编辑过链时就是这个形态，所以默认态不落 metadata 键。

- 会话链是扁平绑定 key 数组，不能引用负载均衡组。组的选择发生在 worker 进程的准入层（`main/service/runtime_service.py` 里 balancer 与并发控制器只在 `execution_mode == "worker"` 时构造），前门跑在没有它们的进程里，给它一个组只会退化成一个占位成员。
- 链上成员被删除或禁用时逐个摘掉、用剩余成员继续；剩余为空才回退全局链。这与固定项失效是同一条判据，两者都不静默改写存储：用户重新启用/重建同名 key 后设置自动恢复。
- 保存会话链会取消该会话的固定模型，两态互斥由 `normalize_model_selection` 保证，不依赖调用方自觉。
- 本地与渠道会话（`ext:` / `china:`）都可固定模型、也可设会话链：渠道会话键即规范会话键，设置接口按会话文件定位，只增删 `model_selection` 键，不动渠道侧自有元数据。
- 只有带会话键的解析跟随会话链：前门回合、迭代/重试边界的链轮换、composer 用量预估、内联工具提醒。`build_chat_model(config, role="ceo")` 那两条进程级车道（`g3ku/runtime/config_refresh.py` 的 provider 重建、`g3ku/runtime/context/summarizer.py` 的资源目录 L0/L1 摘要）没有会话上下文，恒按全局链。
- 解析入口是 `CeoFrontDoorSupport._resolve_ceo_model_refs_for_session`（`g3ku/runtime/frontdoor/_ceo_support.py`），`prepare_turn`、迭代/重试边界的链轮换、composer 用量预估与内联工具提醒都走它，因此上下文窗口判定、多模态闸门与实际 provider 请求始终按同一组 refs 计算。
- 与链变更同一口径：固定/取消固定、会话链增删只作用于边界处重建的下一个请求，不中途热切已在飞的 provider 请求。会话模型的读写接口与前端控件详见 `web-and-admin.md`「Composer Model Mode Panel」。
- 固定的是 `models.catalog[]` 绑定 key，不是 provider/model 字符串：链路仍是 `key -> binding -> provider target`，删除或重建 binding 等价于删除该 key。

### 绑定记录丢失时的降级

`models.catalog[].llmConfigId` 指向 `.g3ku/llm-config/records/<id>.json`。这份记录可以比目录行先消失（凭据目录被清空、从别机导入配置、手工删文件），此时目录行仍然合法，只是这一位发不出去请求。唯一判据是 `Config.model_binding_is_missing()`：只看记录文件在不在，存储本身读不到时一律回答"没丢"——一次读盘故障不该把整条链的成员都摘掉。

四条车道按同一口径处理这种行：

- 运行时解析跳过坏成员：`resolve_role_model_key` 返回链上第一个记录仍在的成员，`build_chat_model` 用它构造 provider 链与默认模型。整条链都不可用时报错点名每个成员 key，并把处置写进「模型配置」。
- 结构视图不动：`get_role_model_keys`、`get_scope_model_chain`、管理面 `roles` / `route_entries` 仍按配置的原始链给出。操作员看得见坏的那一位，才知道要恢复哪条记录、删哪一行；节点车道的 `model_refs` 也因此在跳失败时自动前进到下一位。
- operator 读写面降级而不失败：`facade.list_bindings` / `describe_binding` / `set_binding_enabled` / `delete_binding` 对丢失记录出 `record_missing: true` 的行（目录自带的窗口、重试、名称照出，凭据那一半留空），删除不要求记录存在。`facade.get_binding` 保持抛错——它是运行时解析用的，静默返回空 provider 只会把一次配置故障伪装成一次鉴权失败。
- 前端把 `record_missing` 画成「凭据记录缺失」徽标（配置列表卡片与详情抽屉头各一处）。详情抽屉对这种行不请求必然 404 的记录，否则「删除配置」这条修路够不到。

写车道不认「就地重绑」：`update_config_record` 与 `update_binding` 仍要求目标记录存在，恢复一条丢失记录的正道是配置包导入或密钥代际回滚，否则就是删行后重新添加（删行会连带丢掉链位、绑定名与会话固定，所以先试恢复）。

这条口径的分量在启动路径上：`get_agent()` 解析不到链首就构造不出 agent，`runtime.ready` 落回 false，而记忆、任务、会话三个列表都读 runtime——磁盘上一条没少，界面读成"新项目"。所以 `/api/models` 必须比运行时宽容：它是修复入口，一条坏绑定让整个列表 500 等于既看不到也删不掉。

## 8. secret 的真实去向

配置里的 secret 不一定直接写回文件。

当前机制里：

- `config.json` 里会保留结构性配置
- 真正 secret 通过 bootstrap security overlay 管理
- `LLMConfigFacade` 存 record 时会清洗掉明文 secret，再把 secret 写入 overlay
- 脱敏读面（`GET /api/llm/configs/{id}` 的默认视图、绑定行、runtime config 导出、`_redact_config`）与写回闸门共用 `g3ku/llm_config/export.py` 的 `MASKED_SECRET_VALUE` 一个常量：那个值是"这次没改密钥"的哨兵，不是要落盘的内容。出现第二个掩码字面量时，把脱敏记录读回来再 PUT 回去的调用方会把明文 key 覆盖成掩码字符串本身。

所以：

- 如果你看到某个 config record 没有明文 api key，不代表配置丢了
- 排查模型鉴权问题时，不能只看 JSON 文件
- 覆盖层防护：激活用的 master key 无法解密已存在的覆盖层时，服务进入只读空视图，任何持久化都会被拒绝——错误密钥只能「读不到」，不能「写掉」别人的数据（tests/test_security_overlay_guard.py 锁定该契约）。这个状态本身带读数：`/api/bootstrap/status` 的 `overlay_readable`（`false`＝钥匙与覆盖层不成对）与 `overlay_entries`，`_activate()` 同时打一条 ERROR；`mode=unlocked` 只说内存里有钥匙，不说它开得开东西。
- `auto-unlock.key` 是**某一任主密钥的 bearer 件**，启动顺序里它排在口令前面，所以它比口令更先决定进程用哪把钥匙。`install_master_key()`（配置包导入的那一步）在装好新钥匙后必须把它一起换掉：文件已存在就用新钥匙重写（操作员开过免密，意图保留），不存在就不新建（免密仍是 opt-in）。留着旧件等于让下次启动带着解不开自己覆盖层的钥匙起来——所有 apikey 读成空、所有密钥写入被拒，而界面显示一切正常（tests/test_security_auto_unlock_staleness.py 锁定这两半）。同理，worker 的主密钥是 web 在派生它时用 env 递过去的（`g3ku/web/worker_control.py`），所以换完钥匙必须让 worker 重启一次：旧 worker 的 `refresh_runtime_config` 会在 `task_commands` 里留下 `status=failed / error_text=secret_overlay_decrypt_failed`，那是判据而不是噪声。
- 覆盖层写入耐久性：两条销毁路径都要堵。其一就是上面的错误密钥覆盖；其二是**写失败本身**——`SecretOverlayStore.save()` 走同目录临时文件 + `fsync` + `os.replace()`，绝不用 `write_bytes` 直接落目标（`"wb"` 会先截断再写，一次 ENOSPC 就把覆盖层留成 0 字节，之后解密守卫反过来拒绝一切写入，数据没了且恢复被堵）。`_activate()` 只读不写：解锁/启动不回写覆盖层，一次例行重启因此不等于重写全部密钥。守卫测试同时锁住"写失败后原件不变、不留 `.tmp-*`"和"激活后文件 mtime 不变"。
- 密钥材料有自己的滚动备份与留证：`.g3ku/llm-config-backups/<UTC 时间戳>/` 存最近 12 代完整钥匙材料（主密钥信封、`auto-unlock.key`、`llm-config/index.json`、`records/`、`secret-realms/`），代际只在"值得留"的状态下生成——**非空**的信封、任一 record 文件、任一 realm 覆盖层至少存在一样；指纹与最新一代相同就不生成（每次重启都占一代等于把可用副本轮掉）。每次动作追加一行到 `.g3ku/security-events.jsonl`：`key_store_backed_up` / `key_store_missing_material` / `key_store_initialized` / `key_store_restored` / `key_store_overlay_archived`，带 pid、原因与文件数。
- 0 字节等于损坏，不等于存在：`configured_key_material()` 把"存在但长度为 0"的文件单列成 `empty`，它既不算完好也不生成代际，而 `master.key` 这个文件只要存在过（哪怕是 0 字节）就算"这个项目配过"。**现场信封不可用时 `prune_backups()` 直接不裁剪**——那一刻没有任何一代是冗余的，照保留数剪下去就是把一次可恢复事故剪成不可恢复。restore 的"已有 master.key 就拒绝覆盖"同轴：判据是非空可用，0 字节现件可以被替换。
- 凭据文件的写入一律走 `_atomic_write_bytes`（同目录临时文件 + flush + fsync + `os.replace`，失败时删掉临时件并原样保留旧文件）：覆盖层、主密钥信封 `master.key`、`auto-unlock.key`、迁移后的 `config.json` 与 legacy 状态导出都是这条道。直接 `write_text`/`write_bytes` 会先截断目标再写，一次 ENOSPC 就把钥匙清零，之后解密守卫拒绝一切写入——损坏由写入方式造成，不是磁盘本身吃掉的数据。
- **主密钥文件不见了是一次事件，不是一次新初始化**：启动链路 `auto_unlock_from_env()` 先调 `security.audit_key_store()`，把结论缓存在服务上并让它进 `/api/bootstrap/status` 的 `key_store`（`state` ∈ `ok` / `lost` / `unconfigured`，`lost` 另带缺失路径与最新备份位置）。没有这段时，被清空的凭据目录与从没配过的目录在 UI 里长得一模一样，都是"新项目"，丢的东西无人报数。恢复只由 `negi security restore` 显式触发，且现场仍有 `master.key` 时直接拒绝——`negi security status` / `backup` / `restore` 是操作员入口。
- legacy 迁移删除凭据前有前置条件：`_migrate_legacy_state()` 是合法删掉 `master.key` 的唯一路径，它先留一份代际、再要求备份目录里确实出现 `llm-config/` 副本，否则整笔中止——`copytree` 静默失败过一次就会把"清理明文"变成不可恢复的擦除。
- 覆盖层的 `delete()` 改名不删件（`<name>.removed-<ts>` 并记一条事件）：一份谁也解不开的覆盖层正好记录了"当初期望哪把钥匙"，删掉它就把一次锁死变成一次丢失。
- 残留缺口：写失败时内存视图已经带上新值，只有磁盘回滚了；下一次成功写入会把它带上盘，但失败到那次之间，本进程读到的值与盘上不一致。

## 9. 已退役的 channels / chinaBridge 段

内置中国渠道子系统已整体拆除，渠道接入改由 External Agent API 承担（详见 `external-agent-api.md`）：

- 顶层 legacy `channels.*` 段会被 `load_config` 显式拒绝（提示改走外部桥接）。
- `chinaBridge` 段走迁移型退役：`_migrate_removed_china_bridge_config` 在加载时 pop 该段并置 `changed`，首次加载即裁剪落盘；`_migrate_config` 末尾的兜底 pop 同时化解 secret overlay 孤儿条目（`config.chinaBridge.*`）在 apply 阶段的回灌——解锁态保存后孤儿被永久剪除。两条契约由 `tests/test_china_bridge_removal_migration.py` 锁定。
- 存量配置带旧段不会启动失败，也不需要手工编辑。

## 10. 常见排障入口

### 启动时报配置字段错误

先看：

- `g3ku/config/loader.py`
- `g3ku/config/schema.py`

### Web 里显示没模型可用

先看：

- `models.roles.ceo`
- `models.catalog`
- `g3ku/llm_config/facade.py`

链上有 key、`/api/models` 里对应行带 `record_missing`，说明绑定的凭据记录已不在（不是没配模型）；运行时按「绑定记录丢失时的降级」跳过该成员，处置是重绑或删行。

## 11. 维护高风险点

- `g3ku/config/loader.py`
  同时承担迁移、校验、保存；改动容易破坏老项目兼容。

- `g3ku/config/schema.py`
  是 runtime contract 源头，一旦字段语义改动，前后端与运行时都可能受影响。

- `g3ku/llm_config/facade.py`
  涉及 secret、record、binding、memory target，多条模型链路都会经过这里。

## Deployment Unlock Contract

Container deployment introduces a second bootstrap path besides the interactive unlock UI.

- `G3KU_BOOTSTRAP_PASSWORD` may be provided at process start so web and worker containers can unlock the existing project automatically.
- `G3KU_BOOTSTRAP_MASTER_KEY` is the fast path: it activates the project without any password. The web process hands it to its managed child worker, and the browser's 自动解锁 switch also writes it into its own process environment.

Startup unlock order is fixed: already unlocked → `G3KU_BOOTSTRAP_MASTER_KEY` → the auto-unlock file → `G3KU_BOOTSTRAP_PASSWORD`. `auto_unlock_from_env()` (`g3ku/deployment/runtime_startup.py`) is the single hook every entrypoint calls.

The browser side of the same contract is the project settings dialog, served by `main/api/bootstrap_rest.py`:

- `POST /api/bootstrap/change-password` re-wraps the existing master key under a new password. It requires an unlocked process plus the current password, and it never rotates the key: sessions, the secret overlay and an enabled auto-unlock keep working, while the old password stops being accepted.
- `POST /api/bootstrap/auto-unlock` with `{enabled}` writes or removes `.g3ku/llm-config/auto-unlock.key` (the master key itself, mode 0600) together with the `G3KU_BOOTSTRAP_MASTER_KEY` environment variable. `GET /api/bootstrap/status` reports the result as `auto_unlock`, which is what renders the checkbox state. Enabling requires an unlocked process; disabling never does, so the credential can always be revoked.
- `POST /api/bootstrap/lock` clears only the web process's in-memory master key. Background tasks, sessions and the managed worker keep running; only the browser falls back to the unlock screen. It is not the exit path — `POST /api/bootstrap/exit` is the one that pauses running work and shuts the server down.
- `POST /api/bootstrap/setup` optionally takes `data_dir`, the directory where bulk runtime data will live. It is honored only while `status.mode == "setup"`: the endpoint records it into `.g3ku/data-root.json` before the realm is created, so an installed project cannot be re-pointed here (non-setup answers `data_root_requires_setup`). Candidates must be absolute, writable, outside `.g3ku/`, and not an ancestor of the install root. `GET /api/bootstrap/status` answers it as `data_root` (`data_root` / `source` / `default_root`), which the 项目数据地址设置 input pre-fills from.
- `POST /api/bootstrap/pick-data-dir` opens the OS folder dialog on the web process's machine (Windows + `tkinter`) and answers the chosen path; `status.dir_picker.available` shows or hides that button, so Docker hand-types. The dialog is single-flight (`dir_picker_busy`) and gated to `setup` like `data_dir`, since `/api/bootstrap/*` is reachable while locked.

Treat the auto-unlock file as a bearer credential: whoever can read `.g3ku/llm-config/auto-unlock.key` can unlock the project without a password. That is why it is opt-in, why unchecking deletes both the file and the environment variable, and why a shared `.g3ku/` volume must stay inside the trust boundary of the master key. The write requests `0600`, which is only enforced on POSIX: on Windows the file inherits the directory ACL, so the workspace directory itself is the real boundary there.

Maintainers should keep the persistence boundary straight:

- `.g3ku/config.json` remains the structural project config source of truth
- `.g3ku/llm-config/` still stores provider/binding records
- `.g3ku/secret-realms/` and `.g3ku/llm-config/master.key` still hold bootstrap secret state
- those three stay under the install root regardless of the data root, and so does everything a config bundle writes: `.g3ku/...` entries land under the install root, while `memory/...` and `persona/...` entries land under `agents.defaults.workspace` (and the persona note follows that side's `notes_dir`), which is the install root unless an operator re-pointed it — choosing a data directory therefore moves no secret material and changes no export/import contract (see `operations-and-maintenance.md`「关键状态文件与目录」for the data root itself and its resolution order)

So for container persistence, mounting only `memory/` or `sessions/` is not enough. A containerized project that must keep model bindings, secrets, and unlock state across restart must also persist `.g3ku/`.

If a worker container reports `project_locked` while the web container appears healthy, inspect:

1. whether both containers received the same `G3KU_BOOTSTRAP_PASSWORD`
2. whether the shared `.g3ku/` volume is actually the same volume
3. whether the existing project was already initialized with a bootstrap password rather than left in `setup` mode

## Config Bundle Export And Import

`g3ku/config/config_bundle.py` moves one deployment's configuration面, memory面 and persona面 to another as a single password-encrypted file (`.g3kucb`, written under `.g3ku/config-bundles/`). Three scopes travel, and each one is chosen independently at both ends. `config` is wiring: `config.json`, `resources.state.json`, the `llm-config/` records, `secret-realms/`, and the governance database at `main_runtime.governance_store_path`. `memory` is the memory face: the authoritative store `memory/memory_state.sqlite3`, the resident mirror `memory/MEMORY.md`, and the note bodies under `memory/notes/` and `memory/index/`; the reserved persona note is excluded from this face. `persona` is the persona face: the reserved persona note (`memory/notes/note_persona.md` by default, resolved through that side's `memory_runtime` manifest) plus the images in `my_image/` under the workspace root. No scope carries history — tasks, sessions, transcripts, artifacts and logs are out, and so are the memory queue (`queue.jsonl`), the ops ledger (`ops.jsonl`) and per-session review progress (`review_state.json`), the last one because it is keyed by session ids that only exist on the machine that wrote it. A scope that is not selected is neither read at export nor written at import, and selecting none is refused (`bundle_scope_empty`) instead of quietly packing the config面. Path values are read from the raw `config.json` rather than `load_config()`, because import must also run on a project that is still locked and whose overlay is not yet installed.

A bundle that includes the config面 carries the file entries plus the **active master key itself** — which is why export requires an unlocked process: a bundle is not a copy of `master.key`, it is the key that `master.key` wraps. A memory-only or persona-only package carries no key: those scopes cannot rotate or reinstall one, so leaving the key out keeps a second copy of the deployment's master secret out of the file. What the two lanes change is only which key encrypts the payload.

Export has two lanes behind one checkbox, 使用项目解锁密码. The default lane asks for nothing: the payload is encrypted under the live master key, and the project's password envelope travels in the bundle's outer, unencrypted part so the importer can turn a plaintext password back into that key. That envelope is not secret material — it only maps a password to the key, and it is designed to sit on disk — so shipping it drops the retype without dropping the protection. A project unlocked purely through `G3KU_BOOTSTRAP_MASTER_KEY` has no envelope to ship, and that lane answers `bundle_project_password_unavailable`. Unchecking the box switches to a freshly typed 导出口令, encrypted under `scrypt` over a per-bundle salt using `PASSWORD_KDF`, the same recipe as the bootstrap envelope; that password is stored nowhere.

Neither lane has a strength floor. The dialog says so on the export side instead of leaving it implicit: the password is the bundle's only defence, and because the default bundle carries its own envelope, anyone holding the file can check guesses offline. The same paragraph lists what the bundle contains, so the operator sees the blast radius before pressing 导出.

Two exclusions are load-bearing:

- `llm-config/master.key` stays out of the entry list. Import always writes a fresh envelope under the password just typed, with a new salt, so copying the source file would leave a stale second envelope behind. In the default lane the source envelope still reaches the target — as `unlock_envelope` in the outer part of the bundle, which is where it is designed to sit — because that is what turns the importer's typed password back into the master key.
- `llm-config/auto-unlock.key` stays out. It is a bearer credential, and re-enabling passwordless startup on a new machine is a change the operator has to opt into again. What import does instead of shipping one: `install_master_key()` rewrites a file that already exists so it carries the key it just installed — a stale one would win the boot order and unlock the project with a key that cannot open the realm the same import just wrote. A project with the switch off stays off.

Entry keys live in exactly three root namespaces, `.g3ku/`, `memory/` and `persona/`; `_safe_relative_path()` rejects anything else, which is what keeps a package from addressing an arbitrary path. Config entries are workspace-relative and land at the same relative path on the target. Memory entries are **logical names**: the package always says `memory/MEMORY.md`, `memory/memory_state.sqlite3`, `memory/notes/<name>.md`, `memory/index/<name>.md`, and each side maps those names onto its own landing paths. The mirror and the note bodies follow that side's `memory_runtime` manifest (`tools/memory_runtime/resource.yaml`, `settings.document.memory_file` / `notes_dir`), while the store and the index dir are the memory runtime's fixed anchors; `_memory_landing_paths()` reads the manifest and falls back to the schema defaults when it is missing, so a deployment that re-pointed its notes directory ships from there and a half-installed device still lands them where its runtime will look. Persona entries are logical names too: `persona/note_persona.md` follows the same notes-directory resolution, and `persona/my_image/<name>` lands in that side's workspace-root photo directory; only raster image suffixes are accepted. The mapping is not a file inventory: a note body is only reachable through the store that owns it, so landing it where nothing reads it loses the memory rather than leaving a stray file (ownership contract: 见 `runtime-overview.md`「Memory Runtime State」).

Import is a whole replace per scope, never a merge. The config面 replaces because envelope and overlay swap as a pair: a key that cannot open the installed overlay lands in the read-only unverified state, so a half-applied import would leave the deployment unable to persist anything, and `_preflight()` proves the transported key opens the transported overlay before writing while `install_master_key()` re-locks if activation reports that state. The memory面 replaces because sqlite is that store's only authority — merging would have to reconcile `memory_id` primary keys, note ownership and the refresh/passed counters, and no read path asks for a mixture of two deployments. `_preflight()` also refuses a package whose outer `scopes` and inner entries disagree, so a truncated or hand-edited file cannot half-travel. Replaced files are copied to `.g3ku/config-bundle-imports/<timestamp>/` first and restored if the apply step raises. Outside the persona face, import deletes nothing the package does not carry: note bodies the target owns but no imported row points at stay on disk, and the memory runtime's own orphan sweep is what removes them (合同见 `runtime-overview.md`「Memory Runtime State」). Persona is the set-replacement lane: a target persona note absent from the package is removed and photos not in the package are deleted, after the old note and the whole `my_image/` directory are copied to the import backup; non-image files in that directory are left alone.

The governance database and the memory store travel through SQLite's online backup API in both directions rather than as a swapped file: web and worker keep open connections to them, and replacing the file under a live `-wal` resurrects rows that were deleted. Read-only export is also why a bundle carries no `-wal`/`-shm`.

Three consequences belong in the UI, not in the operator's head:

- Import does not change the project's unlock password. A target that already has an envelope requires its **own** password in the second-stage confirm, whatever scopes were chosen: `unwrap_with_password()` proves the operator knows it before a single file is touched, and `install_master_key()` then re-wraps the imported master key under that same password. The proof is not skipped for a memory-only import — writing this deployment's knowledge has to be started by someone who holds its password. The bundle password only ever unlocks the bundle. A target with no envelope yet (a device that never finished onboarding, a fresh install) has nothing to preserve and adopts the bundle password; the response says which happened through `password_kept`, and the toast repeats it, because "口令改成包口令了" and "口令没变" are two different things an operator has to remember afterwards.
- The managed worker still holds the previous master key in memory until restart, so the import response carries `restart_required`. Until then web runs the new config and the worker the old one.
- A memory-only import replaces the store and the mirror together, because the prompt reads the mirror and the mirror is what a session's resident block is taken from (合同见 `runtime-overview.md`「Memory Runtime State」). Packing only the store would leave the previous deployment's projection standing as the injected text. `restart_required` still applies: the worker's memory thread aligns its boot reconciliation and cached ledger with the new store only after a restart.
- A persona-only import replaces the reserved note and `my_image/` as a set and touches no key or store. The frontdoor head block is adopted per session, so a session that already printed its persona keeps that text until compression reprints it, while a new session reads the imported note.

Routes live at `/api/bootstrap/config-bundle/{export,download,import}` in `main/api/bootstrap_rest.py`; that prefix is the lock middleware's exemption, which is what lets import run against a locked or freshly installed project. Both directions take `include_config` / `include_memory` / `include_persona` (camelCase aliases accepted; the export body also accepts `use_project_password`) and the response reports the chosen `scopes` next to `config_entry_count` / `memory_entry_count` / `persona_entry_count`, so the operator sees which面 actually moved. Export raises `423 project_locked` with no active key. Import reuses the exit flow's running-work gate, but the gate is best-effort and must never block an import: a device that only got through the unlock step has no `config.json` yet, and taking the snapshot goes through `load_config()`, which raises there. Every failure leaves as a snake_case code (`bundle_password_invalid`, `bundle_current_password_required`, `bundle_current_password_invalid`, `bundle_file_invalid`, `bundle_version_unsupported`, `bundle_path_rejected`, `bundle_project_password_unavailable`, `bundle_staging_failed`, `bundle_scope_empty`, `bundle_scope_not_in_package`, `bundle_memory_store_missing`, `bundle_persona_empty`) translated in `api_client.js`, and the post-import runtime refresh reports through `item.refresh` instead of throwing — a 500 after the config面 already landed tells the operator the import failed when it succeeded. Beyond the format `version` that gates readability, the envelope records the producing `app_version` and import echoes it next to the target's own, because "两端同版本" is the first thing a cross-device report has to rule out. The envelope version is 4 when the persona scope travels and stays at 3 for config/memory-only packages, so older builds can still import those. The `scopes` field names the面 it carries; version 3 carries config and memory but has no persona scope, and version 2 has no `scopes` field and means config-only, so files exported by older builds still import. A v3 memory face still contains `memory/notes/note_persona.md` as legacy content; the persona checkbox separates the note only for version-4 files.

## Image Multimodal Binding Flag

`models.catalog[]` carries a second binding-owned chat field: `image_multimodal_enabled` (`imageMultimodalEnabled` in saved JSON / admin payload aliases). It is read as a **conservative chain intersection**: a model-reference chain counts as image-capable only when every resolvable member is image-capable, because whether pixels enter the request body must hold for whichever member this send may end up on. `image_multimodal_chain_decision(app_config, refs)` in `g3ku/config/model_manager.py` is where that judgment is written once; the node loop (`ReActToolLoop._image_multimodal_enabled_for_model_refs`) and the frontdoor (`CeoFrontDoorRuntimeOps._ceo_image_multimodal_enabled_for_model_refs`) keep thin boolean wrappers over it, and `model_config` calls it directly to echo which chains a flag write just affected (second return value = display titles of the blocking members, falling back to the binding key). Unresolvable (deleted/disabled) members are skipped rather than vetoing the chain, and admission never filters group members by image capability.

The per-target judgment is `image_multimodal_target_decision(app_config, ref)` in `g3ku/config/model_manager.py`: it answers `(收不收图, 展示名)` for the binding **this send lands on**, and it is the only image field on the binding. Position inside the request body is not a configurable property — see `web-and-admin.md`「Image Upload Gating」for what the assembly actually emits and which readout tells an operator whether a given upstream can see it.

- Default value is `false`.
- Existing saved models that do not have the field must be treated as `false` at load time; there is no backfill migration that rewrites old configs just to add the default. An unknown key sitting in a saved catalog row is ignored by the model, so removing a field never breaks loading of an old `.g3ku/config.json`.
- The flag belongs to the managed model binding layer, not to the provider config record. It must persist in `.g3ku/config.json` under `models.catalog[]`, and it must not be written into `.g3ku/llm-config/records/*.json`.
- `/api/models` and `/api/llm/bindings` both expose and update it because they are two views over the same binding-owned metadata; `model_config` writes it with the same effect, and the model page renders a checkbox for it in the binding header row.

Runtime gating of image uploads by this flag: 详见 `web-and-admin.md`「Image Upload Gating」.

## Reasoning Context Binding Flag

`models.catalog[]` carries a third binding-owned flag: `reasoning_context_enabled` (`reasoningContextEnabled` in saved JSON / admin payload aliases), **default `true`** — the same no-backfill rule and the same storage location as `imageMultimodalEnabled` (it lives in `.g3ku/config.json`, never in `.g3ku/llm-config/records/*.json`). Absent means on; an operator writes `false` only for a gateway that rejects the field. 落盘判据（关掉的那一行才写 `false`）见「写侧：`save_config` 整写与字段白名单」。

It declares that this binding accepts its own thinking content being replayed back to it as `reasoning_content` on the assistant history row. The runtime writes the field only when **every** binding in the resolved model chain enables it (a binding whose managed profile cannot be resolved counts as off), because a row once persisted is re-sent to whichever chain member answers later hops, and a gateway that rejects the field answers 400/422 — which this codebase treats as a request-shape fault that skips the remaining keys and advances straight to the next model. The write gate lives at the history-append point; the send-side sanitizer only passes the field through, so request hashes, preflight estimates and the wire body stay identical.

The flag is read from the config snapshot held by the constructed chat backend, so switching a binding **off** takes effect on worker restart, not on config hot-refresh. Contract body: 详见 `runtime-overview.md`「思考内容（reasoning）的上下文回放」.

## Model Request Parameter Defaults

Per-model generation parameters (`max_tokens`, `temperature`, `reasoning_effort`) and the per-attempt request timeout (`request_timeout_seconds`) are resolved from the llm-config record's `parameters` and applied to every provider request.

- `max_tokens` always resolves to an explicit value: the per-model `parameters.max_tokens` wins; when the record has none, the runtime falls back to the global default `DEFAULT_MAX_OUTPUT_TOKENS = 65536`. Requests therefore always carry an explicit output cap instead of inheriting the provider-side default (which can silently truncate long generations).
- The engine-global `agents.defaults.maxTokens` (default `65536`) is the CEO-loop fallback; main-runtime nodes resolve per model through `_resolve_model_request_parameters` first.
- `reasoning_effort` uses six managed levels: `none` (deep thinking disabled), `low`, `medium` (default), `high`, `xhigh`, `max`. Per-model `parameters.reasoning_effort` wins over the engine default.
- `none` is a stored value but is never sent to the provider: every provider-facing layer (`chat_backend`, fallback chain, chat adapters, openai/responses providers) omits the `reasoning_effort` field when the resolved level is `none`.
- The model config page stores both fields on the provider record (`parameters.max_tokens` / `parameters.reasoning_effort`), like `context_window_tokens`; the page contract lives in `web-and-admin.md`「Model Config Page And Admin Contract」.
- 两条写车道（管理面 `PUT /api/models/{key}` 与 `model_config` 工具的 `update_model`）共用 `ModelManager.update_model` 的缺席判据：**没交上来的字段保持原值**，哨兵是模块级 `_UNSET`。工具的调用侧按"值不是 `null`"判定这一格是否被交了——模型会把不想改的字段也按参数表列出来并填 `null`，所以 `null` 与"没列出来"同义；要清空只有空串 `""`，且仅对有空值语义的字段成立（`max_tokens` / `temperature` / `context_window_tokens` / `request_timeout_seconds` / `extra_headers`）。`retry_count` 没有清空语义，`""` 与 `null` 都是不动。链侧同理：`model_keys` 与 `route_entries` 互斥，把另一格填成 `null` 仍算同时提供而被拒。
- 单条绑定的局部写不需要调用方补齐字段：`facade.update_binding` 以**现有记录为底**合并 draft（`_merge_draft(current, draft_payload, replace_parameters=False)`），所以只交 `parameters.reasoning_effort` 时 `base_url`、auth、`context_window_tokens` 都按原值留下。配置面字段一旦被触碰，合并出的 draft 要过整份 `ProviderConfigDraft` 校验（chat 能力要求 `parameters.context_window_tokens` 非空），但**写路径不发真实请求**：落盘与否与上游可达性、密钥有效性无关，`Authentication failed …` 只可能来自显式的连接探测。因此把缺席字段填成 `''`/`null` 传下去的调用方，会踩到 `parameters.context_window_tokens is required for chat capability` 与 `base_url: Base URL is invalid` 两类看起来互不相干的报错——它们是同一个成因。
- `add_model` 的 `context_window_tokens` 是无默认值的必填关键字参数（管理面与工具同一条签名）：窗口没有可安全猜的默认，写错会让整条链的发送标尺失真。网页「添加配置」那一档是这条判据的**显示层**例外——表单预填 200K（=200000）等操作员确认或改，接口与工具签名仍不收默认（合同见 `web-and-admin.md`「Model Config Page And Admin Contract」）。
- 实际发出的参数面必须可回读。Chat Completions adapter 把这一跳真正发出的标量（`model` / `max_tokens` / `temperature` / `reasoning_effort` / `tool_choice` / `parallel_tool_calls`，工具只记 `tool_count`）附在回包上，节点台账逐跳落 `sent_max_tokens` 与 `sent_reasoning_effort`；未上报保持 `None`，与合法的 `0` 分得开。Responses adapter 本来就带完整请求体，所以台账侧只取同样这两个标量，正文与工具束留在 actual-request artifact（详见 `context-and-cache-troubleshooting.md`「provider_request_body」）。
- 两条协议道的键名不同，读取侧两套都认：输出上限在 Chat 是 `max_tokens`、在 Responses 是 `max_output_tokens`；思考档位在 Chat 是平铺 `reasoning_effort`、在 Responses 嵌套在 `reasoning.effort`。单一来源在 `g3ku/providers/base.py` 的 `provider_body_output_cap` / `provider_body_reasoning_effort`——只认 Chat 键名会让 Responses 车道的每一跳读成"没声明上限"，截断判档退化成 `unknown`。
- `request_timeout_seconds`（配置页「请求超时时间(秒)」，位于最大输出TOKEN 右侧）是每次 provider attempt 的超时上限，同时约束外层 attempt 看门狗与流式首块/块间空闲超时：留空 = 未配置，回退全局默认 `DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS = 600`；配置则必须 `> 0`。解析优先级：调用方显式传入 → 该模型配置值 → 全局默认，链式回退因此按各模型自己的超时执行。取值真相源与其他参数一致：llm-config record `parameters.request_timeout_seconds`，binding payload 与 catalog 同步。配置里没有独立的 60/120 微默认——任何「未传超时」路径都指向同一个 600 默认。

If a provider reply looks truncated (for example a response ending at exactly the sent `max_tokens` with no tool call), check the node's history record first: 详见 `web-and-admin.md`「Node Detail Error History」。思考档位与最大输出的组合不设保存时校验（`reasoning_effort` 是标签，思考长度没有可标定的上界，且思考与提交体共用同一份 completion 预算——调小 `max_tokens` 只会让撞顶更早，不会限制思考），命中按运行时处置：见 `main-task-runtime.md`「Node-Level Pause and Recovery」。

## Model Retry And Key Rotation Config

每个模型绑定有 `retry_on`（关键词列表）与 `retry_count`（可重试错误的退避重试轮预算）。重试/轮换/退避的**行为契约**见 `runtime-overview.md`「Chat provider 超时与重试边界」；这里只讲配置语义。

- `retry_on` 是**真开关**，区分"未设置"与"显式置空"：省略该字段 → 用默认关键字 `["network","429"]`；显式设为 `[]`/`""` → 无关键字 → 任何错误都不触发退避重试。schema validator（`_normalize_retry_on`）与 `model_manager` 都按此区分，不把显式空值回填成默认。输入按空格/逗号/换行等价分隔解析（配置页以「自动重试错误关键词(空格间隔)」收集）；关键词是单 token，含空白的短语会被拆开。关键词是**第二级判据**：能拿到结构化 HTTP 状态时以状态为准（`RETRYABLE_STATUS_CODES`，单一来源在 `g3ku/providers/base.py`），只有拿不到状态才查这张表；比对前 `_` / `-` / `.` 一律按空格看待，所以 `rate limit` 也能命中上游的 `rate_limit_exceeded`。
- 判定为可重试的错误走**该模型的退避重试轮**：一轮 = 完整轮过该模型所有 key，轮间指数退避加抖动；命中即**不换 key**、也不零等待消费下游模型。轮预算耗尽才前进到链上下一个模型。链报错停止的判据是"链上每个位置都进过一次"，不是各模型预算之和跑完——被判定成不可重试的失败会让那一把键 0 轮直接让位（详见 `runtime-overview.md`「Chat provider 超时与重试边界」）。
- 请求体形状错误只按结构化 HTTP 状态判定（400/422），无文本关键字兜底；status 不可得的错误一律走正常轮换/降级判定。
- 换 key（轮换）只在错误**未命中 `retry_on`、且非请求体形状错误、且非内部运行时错误**时才发生，且为单趟：每个 key 各试一次即前进到链上下一个模型。**配置脚枪**：把 `401`/`403`/`invalid api key` 之类配进 `retry_on`，会让坏 key 被当成"可重试"从而只重试不换 key——坏 key 应靠"未命中 → 换 key"自愈，不要配进 `retry_on`。
- `retry_count`（配置页「重试次数」）是该模型可重试错误的最大退避重试轮数：0/未设置用内置默认 `DEFAULT_RETRYABLE_MODEL_ROUNDS=10`；非可重试错误的轮换恒为单趟、不受该值影响。同一个 key 配置在多个模型上互不影响——轮预算按（模型, key）槽位独立计。**Σ(每模型轮预算 × 该模型 key 数) 只是"每一次失败都被判可重试"时的上界**，不是保证：任何一处判定把失败读成不可重试，那一把键就按单趟让位，实际尝试次数会低于这个上界。
- 负载均衡组内的成员**不继承** `retry_count`：组用 `models.loadBalanceGroups.<key>.maxRetryRounds`（配置里不填按 1；允许任意非负整数，0 与 1 等价——一轮都不重复就让位；只有负数报错）。原因是一个配了 `9999` 或 `9999999` 的成员会在组内永远不让位，组内平级 fallback 随之失效。`retry_count=0` 在这两条车道上含义不同，所以组预算必须写显式值，不能靠「省略字段」落到 10 轮默认。这一格数值对在飞重试的生效时机见本文「模型链变更何时作用于在途回合」。
- 管理面按**单 key 约束**引导配置：`org_graph_llm.js` 创建配置与保存连接信息时拒绝多 key 输入（逗号/换行分隔即报错），提示以「多配置组模型链」实现容量与容灾回退。运行时多 key 轮换代码路径保留以兼容历史存量配置；新配置从配置面即被限定为单 key。

## Frontdoor Context Window Contract

Frontdoor request-size control comes from the selected chat model's `context_window_tokens`.

- Every managed chat model and every `llm-config` chat binding must carry `context_window_tokens`.
- 只有一个权威载体：绑定记录的 `parameters.context_window_tokens`。`models.catalog[].contextWindowTokens` 是同一份值的**副本**，只给配置面与派单侧读，不是第二处真值；每次加载配置都按记录重算它（`g3ku/config/loader.py` 的同步步骤，`requestTimeoutSeconds` 这份副本同一条规则一起收敛；修了几处就同时写一条 `model_catalog_parameter_resynced` 审计事件点名 `model_key/field/catalog/record`——配置加载发生在 loguru 文件 sink 挂上之前，只打 WARN 会在两份运行日志里同时落空），因为存在只写记录、碰不到 catalog 的编辑道（`PUT /llm/configs/{config_id}` → `update_config_record`）。副本与记录分叉过的那类后果：发送压缩按记录算触发线、派单按副本算"谁塞得下"，两个数中间那段体量既不压缩也无人可派，请求会在共享回合队列里把队头独占成只剩它自己。
- The value is runtime-authoritative: CEO/frontdoor resolves the currently selected model, reads its `context_window_tokens`, and uses that number for pre-send checks. 尺子 = 这一跳可能被达到的成员里**最小**的那颗窗口：节点侧由准入按与尺寸无关的资格预测出那一跳的目标组，取该组最小成员；CEO/frontdoor 与记忆车道没有回合准入，退回整条会话链/模型链的最小值。合同本体（为什么是最小不是最大、预测与授予如何共用一份资格判据）在 `context-and-cache-troubleshooting.md`「节点侧排查要点」。配置面后果：链上任何一颗成员的窗口都可能决定本轮压缩目标；同一个"本轮多大"还会交给发送层筛链位——声明窗口塞不下的成员不会首发（见 `runtime-overview.md`「Chat provider 超时与重试边界」）。所以把某颗成员的窗口填小，会同时压低压缩目标与降低它在链上的被选次序。
- 因此**组内成员窗口不齐会压低整组压缩目标**：往一个组里加一颗小窗口模型，整组按那颗压缩；把最小的那颗调大，整组的压缩目标当场跟着变大（下一跳生效，已经压掉的上下文不会还回来）。配置面只在组弹窗里、成员清单之上给一行「组内最大上下文对齐: <值>」，值就是那颗最小成员的窗口；成员窗口不一致时这行转成警示档，本组最大写在 hover 上。组卡与链上的组卡都不显示它——这层连带只在正勾成员的那一刻有用。这两个数只在这里给人看，不进故障正文（链级数字会把人引向链上别的成员）。
- For CEO/frontdoor, "runtime-authoritative" explicitly means the current live config revision from `get_runtime_config(...)`. Maintainers should not treat `loop.app_config` as an equivalent source of truth for send-time context-window decisions, because it may lag behind recent admin/model edits.
- There is no fallback to `loop.context_length` and no default floor.
- Inline legacy model payload migration must preserve `contextWindowTokens`; otherwise later `/api/models` reads and role-chain validation will misreport the model as missing a context window.

### Save-Time And Run-Time Validation

- Model create/update requires `context_window_tokens > 25000`.
- Role-chain batch save fails if any referenced model is missing a valid `context_window_tokens`.
  - 角色链校验在副本仍缺失或 ≤25000 时会从绑定记录补一份（这条只覆盖"老安装升级后没落盘"的形状）；两本账真正收敛的地方是加载时的按记录重算，见本文「Frontdoor Context Window Contract」。
- Old stored models may still exist without that field, but if one is actually selected at runtime the turn fails fast instead of sending with an implicit unlimited window.

### What To Check When It Breaks

If an operator reports frontdoor send failures after a model or chain change, check in this order:

1. The selected model binding in `/api/models` really exposes `context_window_tokens`. 窗口读数与**链首 provider 能否建出来**是两根轴：链首密钥缺失/记录坏时 `resolution_error` 带着身份故障原话，而尺子仍按目录与准入预测算出来的窗口返回。把这种形状当成"窗口未知"交回发送预检，暂停正文会指向窗口数字，而真正要修的是密钥那一行。
2. The saved `llm-config` record under `.g3ku/llm-config/records/*.json` kept the field during migration.
3. The role chain only references models that have a valid window configured.
4. The actual provider request estimate crossed the model's window.
5. 声明值大于真实上限时**没有任何错误信号**：网关超窗不报错，只把 completion 预算裁成 `真实窗口 − 输入`，所以回包表现为 `finish_reason=length` 且 `output_tokens < sent_max_tokens`。核对时用节点自己的观测下界：`max_observed_request_span_tokens(task_id, node_id)` 取该节点台账里 `有效输入 + 输出` 的最大实测和（走 `task_model_calls` 的 task_id 索引，不全扫）。它是"至少装下过多少"的下界而不是真值上界；同一份声明值在发送侧（`resolve_send_model_context_window_info` 读记录 `parameters.context_window_tokens`）与派单过滤（`model_route` / 负载均衡成员）同源，改配置前先确认改的是记录那份——`models.catalog[].contextWindowTokens` 只是配置面副本。窗口虚高还会把两道闸门一起顶到墙外：发送压缩触发线（`0.80 × 窗口 × 0.95`）与节点分块压缩的进入条件都比的是这个数。除这条节点下界外，运行时还按**成员**收集同一把发送前尺子上的两臂读数（该成员答过的最大 vs 它被拒时的最小），成立时把区间连同声明窗口与当轮触发线一起落成一行可数报告；它只报数，不改路由、不否决准入、不写这份记录，合同与判据在 `context-and-cache-troubleshooting.md`「节点 400 不一定是 payload 畸形」那条坑里。

Behavior once the estimate crosses the window: 详见 `runtime-overview.md`「Frontdoor Context Compression」.

## Memory Runtime Settings Anchor

`tools/memory_runtime/resource.yaml` is the runtime settings anchor for long-term memory. It holds the Markdown notebook, durable queue, and memory-agent settings.

Settings surface:

- `document.*` controls the Markdown notebook layout, including `memory/MEMORY.md`, `memory/notes/`, the summary character limit, and the full document character ceiling.
  - The default `document.summary_max_chars` is `300`.
  - `document.compress_trigger_chars` / `document.compress_target_chars` are the consolidation thresholds and are measured against the resident memory entries only: crossing the trigger queues a `consolidate` batch at the queue head instead of shrinking the snapshot in place, and the permanent consolidation index counts toward neither threshold nor `document_max_chars`. See `runtime-overview.md`「Memory Runtime State」.
  - `document.compress_trigger_chars` and `document.compress_target_chars` define the post-commit snapshot compaction thresholds.
- `queue.*` controls the single durable queue and its failure parking store, including `memory/queue.jsonl`, `memory/ops.jsonl`, `memory/failed.jsonl`, batch size, max wait time, and the ordinary-turn review window size. `queue.review_interval_turns` is the per-session ordinary-turn review window size, defaulting to `5`. `queue.auto_requeue_on_success` (default `true`) enables the success-signal auto requeue: each applied batch requeues the oldest parked `provider_error` record at the queue tail; `window_refusal` (local pre-send capacity refusal), `agent_fault` (deterministic write fault) and protocol-violation records never auto-requeue. Parking/requeue semantics live in `runtime-overview.md`「队列状态机与失败停车语义」.
- `agent.*` controls the dedicated memory-maintenance worker behavior.
  - `agent.repair_attempt_limit` is the number of repair retries after the first validation failure; total model attempts per batch = `1 + repair_attempt_limit` (default `2`, i.e. 3 attempts).
  - When every attempt still fails validation, the runtime falls back to a best-effort "minimal compression" write (uses each item's `minimal_memory` as the stored summary); the applied history row records `fallback: "minimal_compression"`. If that fallback is not possible (or the provider fails), the batch parks into `memory/failed.jsonl` instead of being terminally discarded.
- `mode`, `backend`, `bootstrap_mode`, and `compat.dual_write_legacy_files` are not part of the active memory runtime settings surface.

Project-config side keys:

- `models.roles.memory` is the dedicated model chain for the internal memory agent.
- `agents.roleIterations.memory` controls the memory agent's model-call round cap.
- `agents.roleConcurrency.memory` is fixed to `1`; it is persisted for config/UI symmetry but is not an operator-tunable parallelism knob.
- `models.roles.memory` may be empty, but when it is non-empty every referenced binding must have `capability=chat`. The admin route rejects non-chat bindings for the memory role.
- Unlike `ceo`, `execution`, and `inspection`, the `memory` role is allowed to be empty; that does not fail config load. The first-run onboarding chain gate is the one place that additionally requires a non-empty `memory` chain when persona input exists; that gate lives in the web-side onboarding flow and is not a config-load requirement (详见 `web-and-admin.md`「First-Run Onboarding And Persona Contract」).
- If the queue head is already inside `processing` when an operator changes `models.roles.memory`, the already dispatched provider call is not hot-swapped. Before the next internal memory repair attempt, the runtime re-reads the latest revision and re-resolves the memory model chain.

The active internal memory writer prompt is the file-backed runtime asset `main/prompts/memory_agent.md`.

When debugging "memory queue stuck" reports, check both layers in order:

1. `models.roles.memory` / `agents.roleIterations.memory` in `.g3ku/config.json`
2. `tools/memory_runtime/resource.yaml` queue/document limits

Do not assume a valid CEO model chain implies a valid memory-agent chain; the memory worker does not fall back to CEO.

Queue execution and writer workflow: 详见 `operations-and-maintenance.md`「Memory Queue Workflow」.
