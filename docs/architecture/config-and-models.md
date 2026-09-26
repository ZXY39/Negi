# G3KU 配置与模型系统说明

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

- 默认 workspace
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

`catalog[]` 条目另有 `quotaPoolKey`：operator 显式声明「这几条 binding 共享同一个上游配额账户」。不填时运行时只按解析到的 endpoint + API key 指纹自动合并；不按 provider 名称猜测共享。

### `providers`

保存 provider 级基础信息，例如：

- `api_key`
- `api_base`
- `extra_headers`

### `web`

定义 Web bind host / port。

### `resources`

定义 skills / tools 资源目录与 reload 策略。

### `main_runtime`

定义任务运行时存储与调度参数。其中 `main_runtime.duplicate_precheck.llm_review_enabled`（默认 `true`）控制 `create_async_task` 重复预检的第二层语义审查：关闭后只保留确定性精确匹配层，模糊重复与 `reject_use_append_notice` 识别随之消失，预检对放行结果 fail-open。字段契约与拒绝语义详见 `tool-and-skill-system.md`「fixed builtin tools」。

`main_runtime.disk_guard` 子节控制磁盘写保护与治理（行为契约见 `runtime-overview.md`「磁盘写保护与治理」）：`write_guard_enabled`（默认 `true`；关闭后写异常恢复原样上抛、不做应急预算预检）、`emergency_min_bytes`（默认 300MB）与 `emergency_min_ratio`（默认 0.01，紧急线取两者较大值）、`usage_ttl_seconds`（水位探测缓存 TTL，默认 5s）、`artifact_gzip_threshold_bytes`（默认 1MiB；`<=0` 关闭 artifact gzip 压缩）、`terminal_cleanup_enabled`（默认 `true`；关闭后任务终态不自动清理确定不再使用的数据）、`terminal_temp_dir_cleanup_enabled`（默认 `false`；开启后终态硬删 `temp/tasks/<id>`）、`auto_pause_enabled`（默认 `true`；关闭后紧急态只检测告警不自动暂停任务）、`emergency_streak_samples` / `emergency_recovery_samples`（进入/解除紧急态的连续采样拍数，默认 3/5）、`alert_on_disk_emergency`（默认 `true`）、`detail_retention_days`（终态任务大行保留天数，默认 `0`=停用裁剪——任务明细与任务同生命周期，仅随手动删除清除；配置 `>0` 恢复按天裁剪）。磁盘治理没有清理线/自动删任务相关配置；删除台账保留期（7 天）是 `runtime_service` 模块常量，不进配置。`G3KU_*` 同名环境变量仅作测试与应急覆盖，配置源真值以本子节为准。

### `external_api`

定义 External Agent API（`/api/v1`）的启用与桥接凭据：`enabled`（默认关）、`tokens`（bridge_id → `{token, label, enabled}`）、`eventBufferSize`。token 密文走 bootstrap secret overlay 三件套（保存时剥离进覆盖层、落盘配置只留占位、解锁时回填）。字段语义与鉴权契约详见 `external-agent-api.md`「启用与鉴权」。

### `cron`

定义 cron 调度器的投递看门狗预算：`dispatchTimeoutSeconds`（默认 1800，单次 job dispatch 的等待上限，超时会 dump 挂起任务的 await 链、取消 dispatch 并把该次运行收尾为 `timeout`；`<=0` 关闭看门狗回到无限等待）与 `dispatchCancelGraceSeconds`（默认 10，取消后等待任务展开的宽限，超时视为抗取消任务并脱离调度器放弃）。默认预算刻意大于最长合理回合（provider 单次尝试上限 + 退避重试轮），只兜底真正楔死的 dispatch。该节属于加载器「显式字段校验」的豁免前缀：存量 `config.json` 不写 `cron` 节也能启动，取值回落到 schema 默认。行为契约详见 `heartbeat-system.md`「Cron Reminder Contract」。

### `stt`

定义本机语音识别（默认开启，关掉它是操作员说"别碰我这条车道"）：`enabled`、`model`（`tiny`/`base`/`small`）、`language`、`threads`、`maxAudioSeconds`、`minRmsDbfs`、`timeoutSeconds`、`simplifyChinese`、`modelDir`（默认 `.g3ku/stt`，跟随数据根）、`binaryPath`、以及分发四件 `binaryReleaseTag` / `binarySha256` / `binaryDownloadBaseUrl` / `modelDownloadBaseUrl`。没有密钥字段，因此不参与 secret overlay 的抽取/剥离三件套。

默认开的是**许可**，不是那 157 MB 的下载：二进制与模型仍要显式取得（`g3ku stt prepare`，或网页首次点麦克风时按需下载），所以一台没下过东西的机器上 `ready` 仍是 false，引擎不会自己去联网。反过来，已经保存过配置的旧设备里 `enabled: false` 是写死在 `config.json` 里的，不会随默认值翻转——那是它自己的显式选择。

该节属于加载器「显式字段校验」的豁免前缀：存量 `config.json` 不写 `stt` 节也能启动，取值回落到 schema 默认。但 `_runtime_config_payload` 是显式白名单序列化，`stt` 的每个字段都必须同时出现在那里，否则任何一次配置保存都会把整段静默丢掉（`g3ku stt prepare --enable` 就写不进去）。引擎形态、就绪判定与默认值的实测依据详见 `speech-to-text.md`。

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

还要额外记住一个运行时边界——模型链变更何时作用于在途回合：

- 配置刷新不会把一个“已经发出去的单次 provider 请求”中途热切换到新模型；切换只作用于边界处重建的下一个请求。
- CEO/frontdoor 在每次 `call_model` 迭代边界（含 provider-failure retry / empty-response retry 边界）对比 runtime revision：revision 变化时重新解析当前角色模型链并写回轮状态，下一次 provider 请求、上下文窗口估算与绑定模型链的能力判定（如 `content_open` 的多模态闸门）都跟随新链。轮状态用 `model_refs_revision` 记录解析时的 revision，作为下一次边界对比的基线。
- 因此 `model_config set_scope_chain` 之后，同一轮的下一次模型调用即用新链；在同一步把切链与被门控的工具调用作为并行工具调用发出时，闸门仍按切换前的链判定。
- Main runtime 节点的模型链在任务运行时 chat 后端的每个链轮边界活解析（`model_refs_resolver` 读取当前角色链），路由/绑定变更在下一个链轮即生效，不必等下一个回合；可重试失败退避等待结束后同样先重新解析链再继续重试。**链在重试途中被改写不会中止回合**：退避边界发现 runtime revision 变化且能解析出与在用链不同的新链时，重试循环丢弃旧链的已试集合、刷新 revision 基线并从新链链首重新评估，成功即正常返回；链未真正变化时只刷新基线，继续原有重试账本，不在同一 revision 上空转。按链首重启意味着新链里此前被跳过的模型重新获得机会。`DEFAULT_MAX_CHAIN_CHANGE_RESTARTS`（`g3ku/providers/fallback.py`）跨重启累计，只兜底"链反复变化且始终不成功"的病态抖动，达到上限后落既有链耗尽终态。CEO/frontdoor 与二者之上还有各自的回合级守卫：`react_loop` 在链耗尽错误处按 `provider_retry_invalidation` 重建回合，frontdoor 在 provider 失败/空响应边界重新解析角色链，二者都只在 `ensure_runtime_config_current` 报告配置确有前进时触发。memory queue 内部 agent 看的不是 CEO/node 的 provider retry，而是 memory 自己的同批次 validation/repair 重试点，普通 review window 不经过单独的 `assess -> apply` 交接。

配置刷新同时重载节点侧的路由结构：`models.roles` / `models.loadBalanceGroups` 变化后，worker 会重建 `execution` / `inspection` 的 route plan 并把组定义连同新的 revision 交给负载均衡器（契约见 `runtime-overview.md`「节点模型路由与准入绑定」）。revision 只作观测，不参与重绑判定：刷新只解绑「绑定成员已被移出组」的那些节点，其余绑定与最近的请求速率、429 惩罚一律保留——那两项描述的是上游，与本地配置有没有被改过无关。因此一次与路由无关的保存（改 prompt、改渠道设置）不会打断任何节点的缓存粘性。

维护上把这理解成“迭代/重试边界上的重建”，而不是“请求中途热切模型”。如果用户反馈“改完模型链后旧模型还在用”，重点检查：

1. 对应进程是否真的执行到了 runtime refresh（日志 `Loop runtime config refreshed`）
2. 问题是否发生在单次 still-in-flight 的 provider request 内（该请求不可热切），还是跨过后续迭代边界后仍未换链
3. 当前运行路径是 CEO/frontdoor、main runtime worker/node，还是 memory queue 内部 worker

## 5. 模型系统不是只靠 `config.json`

这是新人最容易误解的一点。

G3KU 的模型系统分两层：

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
- 为管理面「添加模型」流程提供 draft 校验、连接探测、最大并发探测与供应商模型目录拉取（管理面契约详见 `web-and-admin.md`「Model Config Page And Admin Contract」）

协议（`protocol_adapter`）是记录级派生字段：它由 `provider_id` 命中的 provider 模板唯一决定，draft 里同名参数不参与解析，因此切换协议等于换模板而不是写一个独立字段；归一化后的值随 runtime target 导出，由 `g3ku/providers/provider_factory.py` 决定构建 Chat Completions 还是 Responses provider。模板同时决定 `parameters` 的字段集合与 `reasoning_effort` 白名单，两者必须保持一致，否则管理面会给出保存得了但校验不过的字段。

Responses 协议的请求体只带各家 `/responses` 共同支持的字段：`text.*` 这类 OpenAI 扩展会被代理到 Chat Completions 后端的供应商整单拒绝，`instructions` 同样会被判成 `inference request is invalid`，因此系统提示词只以 `input` 首位的 `[SYSTEM]…[END SYSTEM]` user 块送达，深度思考走 `reasoning.effort`。连接探测发的是同一个请求形状——一条 ping 加一个走各自协议 normalizer 的占位函数工具——并且模型目录可读不算通过：目录之后还要过一次真实形状的推理，被拒时把上游原文带回消息，字段级不兼容因此在保存前就暴露，而不是等第一个真实回合。管理面「完整上下文」侧的预览请求体与真实发送体保持同形状，否则预览给出的字段清单不可信。

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

绑定 key 是稳定主键：它唯一标识 `models.catalog[]` 条目，同时被 `models.roles.*` 和 `agents.multi_agent.orchestrator_model_key` 引用。管理面创建 binding 时以记录的 `default_model` 为基底自动生成 key，遇到同名模型时追加数字后缀去重，因此 key 不再等于模型名，不同供应商可以添加同名模型。展示标题的优先级是绑定级 `name` > 记录的 `default_model` > `key`：`name` 是 `models.catalog[]` 条目的绑定层字段，空值为「未命名」，展示回退到 `default_model`，写入空 `name`/删除 `name` 即回到回退展示而不改写 key；非空 `name` 在创建/编辑 binding 时做大小写不敏感的全局去重（排除自身），`/api/models` 与 `/api/llm/bindings` 两个视图都读写该字段。编辑 `default_model` 或 `name` 都会更新展示标题而不改写 key；命名与展示职责详见 `web-and-admin.md`「Model Config Page And Admin Contract」。

### 角色路由：有序 fallback 与负载均衡组

一条角色链是**有序 route entry 列表**，链上顺序就是 fallback 优先级。CEO/frontdoor 的 `model_refs` 不被过滤也不按 provider 模板或协议能力重排；只有链首失败后才前进到下一个 entry（重试与轮换预算见 `runtime-overview.md`「Chat provider 超时与重试边界」）。

`execution` / `inspection` 额外允许把一跳写成负载均衡组（`loadBalanceGroups` 引用）。语义差别是维护者最容易读错的地方：

- 组内成员**平级**，`modelKeys` 的书写顺序只用于展示与稳定序列化，不参与选择；链上 entry 的顺序才参与。
- `models.roles.*` 与所有「候选展开」出口（`get_role_model_keys`、`Config.get_scope_model_chain`、管理面 `roles` 字段、`facade.get_routes`）都是**候选视图**：组被摊成成员列表。任何把「候选数组第一项」当成实际执行模型的代码都只在 legacy 纯 direct 链上成立；含组时真正的成员由准入层的绑定决定（契约见 `runtime-overview.md`「节点模型路由与准入绑定」）。
- `route_entries` 才是有序结构。管理面读写它，`roles` 保留旧形状给存量客户端。
- 落盘形状：一条链全是 direct 时继续写字符串数组（存量 `config.json` 零 diff），一旦出现组 entry 才整条改写成对象数组。读侧两种形状都吃。
- `ceo` / `memory` 出现 `load_balance` 会被直接拒绝（负载均衡组当前仅支持 execution/inspection）。记忆车道有固定单并发与 chat capability 契约，CEO 有会话固定模型与缓存键约束，都不能被组语义覆盖。
- `mainRuntime.modelRouteLoadBalanceEnabled = false` 是回滚闸门：含组的链按配置顺序摊平成 direct 候选，准入与发送侧一起回到有序链行为。
- 组是**全局资源，不属于任何一条链**：`_prepare_scope_route_update` 在落链之前先落整份 `models.loadBalanceGroups`，所以一次 scope 保存可以只带 `model_keys` 加一份组集合（链里还没有组也行）——管理面的组配置列因此允许「先建组、之后再拖进链」。也正因每个 scope 的保存都整份替换该字典，客户端必须交**完整**组集合，交子集会把没在这条链上用到的组删掉。成员为空的组不落盘（`modelKeys` 必填），因此链指向一个空组时会得到可读的 `Unknown load balance group`，而不是静默少一跳。
- 组名是引用位的一部分：改名要同时重写所有链上的 `group:<key>` 记号（管理面在前端一次改完），删除组要连带摘掉引用它的链位。组名撞模型 key 由 schema 直接拒。

两条与序列化器绑定的维护陷阱：

- `models.loadBalanceGroups` 在加载器的「显式字段」校验里是**豁免前缀**。它不豁免的话，任何一份没有该节的存量 `config.json` 都会在下次加载时被要求填写组字段。
- 写链必须走 `Config.set_role_model_keys` / `set_role_model_routes`。字段类型是 `ModelRouteEntry` 之后，就地 `append` 裸字符串不会被 pydantic 拦下（未开 `validate_assignment`），但会让改名、删除这类按 key 比较的写路径静默失配。改名要同时命中链上的 direct entry 和组内成员（`rename_model_key_in_routing`）；把模型从组里删到空是报错而不是顺手删组。

- provider 的 `supports_prompt_caching` 不参与模型选择。它描述的是这条车道转不转发 `prompt_cache_key` 这个请求字段，而不是这个模型吃不吃得到缓存——命中来自网关侧自动前缀缓存，两家车道都会回报 `cached_tokens`（取证口径见 `context-and-cache-troubleshooting.md`「Family 与 key 合同」）。把它当成路由门控会把不具备该字段的模型整段移出链，连带取消它们的容灾资格。
- 「面板显示的模型不像链首」按两个字段判读：preflight diagnostics 的 `resolved_model_key` 是本轮生效的绑定 key，`provider_model` 是 provider 侧模型名。多条绑定可以共用同一个 `provider_model`，只有绑定 key 能区分它们。含组时 `resolved_model_key` 就是被选中的那个组成员。

### 会话级固定模型优先于角色链

Leader（CEO/frontdoor）解析本轮模型引用时，先读会话元数据的 `model_selection`：`{"mode": "chain"}` 走 `models.roles.ceo`；`{"mode": "model", "model_key": "..."}` 时本轮 `model_refs` 是该固定模型（单元素），不再走模型链。

- 固定项被删除或禁用即视为失效：运行时回退模型链继续发请求，而不是带着不可用模型发请求；失效不静默改写存储，用户重新启用/重建同名 key 后固定关系恢复。
- 本地与渠道会话（`ext:` / `china:`）都可固定：渠道会话键即规范会话键，设置接口按会话文件定位，只增删 `model_selection` 键，不动渠道侧自有元数据。
- 解析入口是 `CeoFrontDoorSupport._resolve_ceo_model_refs_for_session`（`g3ku/runtime/frontdoor/_ceo_support.py`），`prepare_turn`、迭代/重试边界的链轮换、composer 用量预估与内联工具提醒都走它，因此上下文窗口判定、多模态闸门与实际 provider 请求始终按同一组 refs 计算。
- 与链变更同一口径：固定/取消固定只作用于边界处重建的下一个请求，不中途热切已在飞的 provider 请求。会话模型的读写接口与前端控件详见 `web-and-admin.md`「Composer Model Mode Panel」。
- 固定的是 `models.catalog[]` 绑定 key，不是 provider/model 字符串：链路仍是 `key -> binding -> provider target`，删除或重建 binding 等价于删除该 key。

## 8. secret 的真实去向

配置里的 secret 不一定直接写回文件。

当前机制里：

- `config.json` 里会保留结构性配置
- 真正 secret 通过 bootstrap security overlay 管理
- `LLMConfigFacade` 存 record 时会清洗掉明文 secret，再把 secret 写入 overlay

所以：

- 如果你看到某个 config record 没有明文 api key，不代表配置丢了
- 排查模型鉴权问题时，不能只看 JSON 文件
- 覆盖层防护：激活用的 master key 无法解密已存在的覆盖层时，服务进入只读空视图，任何持久化都会被拒绝——错误密钥只能「读不到」，不能「写掉」别人的数据（tests/test_security_overlay_guard.py 锁定该契约）
- 覆盖层写入耐久性：两条销毁路径都要堵。其一就是上面的错误密钥覆盖；其二是**写失败本身**——`SecretOverlayStore.save()` 走同目录临时文件 + `fsync` + `os.replace()`，绝不用 `write_bytes` 直接落目标（`"wb"` 会先截断再写，一次 ENOSPC 就把覆盖层留成 0 字节，之后解密守卫反过来拒绝一切写入，数据没了且恢复被堵）。`_activate()` 只读不写：解锁/启动不再回写覆盖层，一次例行重启因此不再等于重写全部密钥。守卫测试同时锁住"写失败后原件不变、不留 `.tmp-*`"和"激活后文件 mtime 不变"。
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
- those three stay under the install root regardless of the data root, and so does every path inside a config bundle — choosing a data directory therefore moves no secret material and changes no export/import contract (see `operations-and-maintenance.md`「关键状态文件与目录」for the data root itself and its resolution order)

So for container persistence, mounting only `memory/` or `sessions/` is not enough. A containerized project that must keep model bindings, secrets, and unlock state across restart must also persist `.g3ku/`.

If a worker container reports `project_locked` while the web container appears healthy, inspect:

1. whether both containers received the same `G3KU_BOOTSTRAP_PASSWORD`
2. whether the shared `.g3ku/` volume is actually the same volume
3. whether the existing project was already initialized with a bootstrap password rather than left in `setup` mode

## Config Bundle Export And Import

`g3ku/config/config_bundle.py` moves one deployment's configuration面 to another as a single password-encrypted file (`.g3kucb`, written under `.g3ku/config-bundles/`). It covers wiring, not history: `config.json`, `resources.state.json`, the `llm-config/` records, `secret-realms/`, and the governance database at `main_runtime.governance_store_path`. Tasks, sessions, transcripts, artifacts and logs are out of scope. Path values are read from the raw `config.json` rather than `load_config()`, because import must also run on a project that is still locked and whose overlay is not yet installed.

Every bundle's encrypted payload carries the file entries plus the **active master key itself** — which is why export requires an unlocked process: a bundle is not a copy of `master.key`, it is the key that `master.key` wraps. What the two lanes change is only which key encrypts that payload.

Export has two lanes behind one checkbox, 使用项目解锁密码. The default lane asks for nothing: the payload is encrypted under the live master key, and the project's password envelope travels in the bundle's outer, unencrypted part so the importer can turn a plaintext password back into that key. That envelope is not secret material — it only maps a password to the key, and it is designed to sit on disk — so shipping it drops the retype without dropping the protection. A project unlocked purely through `G3KU_BOOTSTRAP_MASTER_KEY` has no envelope to ship, and that lane answers `bundle_project_password_unavailable`. Unchecking the box switches to a freshly typed 导出口令, encrypted under `scrypt` over a per-bundle salt using `PASSWORD_KDF`, the same recipe as the bootstrap envelope; that password is stored nowhere.

Neither lane has a strength floor. The dialog says so on the export side instead of leaving it implicit: the password is the bundle's only defence, and because the default bundle carries its own envelope, anyone holding the file can check guesses offline. The same paragraph lists what the bundle contains, so the operator sees the blast radius before pressing 导出.

Two exclusions are load-bearing:

- `llm-config/master.key` stays out of the entry list. Import always writes a fresh envelope under the password just typed, with a new salt, so copying the source file would leave a stale second envelope behind. In the default lane the source envelope still reaches the target — as `unlock_envelope` in the outer part of the bundle, which is where it is designed to sit — because that is what turns the importer's typed password back into the master key.
- `llm-config/auto-unlock.key` stays out. It is a bearer credential, and re-enabling passwordless startup on a new machine is a change the operator has to opt into again.

Import is a whole replace, never a merge, because envelope and overlay swap as a pair. A key that cannot open the installed overlay lands in the read-only unverified state, so a half-applied import would leave the deployment unable to persist anything: `_preflight()` proves the transported key opens the transported overlay before writing, and `install_master_key()` re-locks if activation reports that state. Replaced files are copied to `.g3ku/config-bundle-imports/<timestamp>/` first and restored if the apply step raises.

The governance database travels through SQLite's online backup API in both directions rather than as a swapped file: web and worker keep open connections to it, and replacing the file under a live `-wal` resurrects rows that were deleted. Read-only export is also why a bundle carries no `-wal`/`-shm`.

Two consequences belong in the UI, not in the operator's head:

- After import the target unlocks with the **bundle password**. With the default export path that is the same password the source used; with a custom one the target's previous password stops working.
- The managed worker still holds the previous master key in memory until restart, so the import response carries `restart_required`. Until then web runs the new config and the worker the old one.

Routes live at `/api/bootstrap/config-bundle/{export,download,import}` in `main/api/bootstrap_rest.py`; that prefix is the lock middleware's exemption, which is what lets import run against a locked or freshly installed project. Export raises `423 project_locked` with no active key. Import reuses the exit flow's running-work gate, but the gate is best-effort and must never block an import: a device that only got through the unlock step has no `config.json` yet, and taking the snapshot goes through `load_config()`, which raises there. Every failure leaves as a snake_case code (`bundle_password_invalid`, `bundle_file_invalid`, `bundle_version_unsupported`, `bundle_path_rejected`, `bundle_project_password_unavailable`, `bundle_staging_failed`) translated in `api_client.js`, and the post-import runtime refresh reports through `item.refresh` instead of throwing — a 500 after the config面 already landed tells the operator the import failed when it succeeded. Beyond the format `version` that gates readability, the envelope records the producing `app_version` and import echoes it next to the target's own, because "两端同版本" is the first thing a cross-device report has to rule out. UI surface: 详见 `web-and-admin.md`「Frontend Theme And Layout Contract」配置段.

## Image Multimodal Binding Flag

`models.catalog[]` carries a second binding-owned chat field: `image_multimodal_enabled` (`imageMultimodalEnabled` in saved JSON / admin payload aliases).

- Default value is `false`.
- Existing saved models that do not have the field must be treated as `false` at load time; there is no backfill migration that rewrites old configs just to add the default.
- The flag belongs to the managed model binding layer, not to the provider config record. It must persist in `.g3ku/config.json` under `models.catalog[]`, and it must not be written into `.g3ku/llm-config/records/*.json`.
- `/api/models` and `/api/llm/bindings` both expose and update this field because they are two views over the same binding-owned metadata.

Runtime gating of image uploads by this flag: 详见 `web-and-admin.md`「Image Upload Gating」.

## Model Request Parameter Defaults

Per-model generation parameters (`max_tokens`, `temperature`, `reasoning_effort`) and the per-attempt request timeout (`request_timeout_seconds`) are resolved from the llm-config record's `parameters` and applied to every provider request.

- `max_tokens` always resolves to an explicit value: the per-model `parameters.max_tokens` wins; when the record has none, the runtime falls back to the global default `DEFAULT_MAX_OUTPUT_TOKENS = 65536`. Requests therefore always carry an explicit output cap instead of inheriting the provider-side default (which can silently truncate long generations).
- The engine-global `agents.defaults.maxTokens` (default `65536`) is the CEO-loop fallback; main-runtime nodes resolve per model through `_resolve_model_request_parameters` first.
- `reasoning_effort` uses six managed levels: `none` (deep thinking disabled), `low`, `medium` (default), `high`, `xhigh`, `max`. Per-model `parameters.reasoning_effort` wins over the engine default.
- `none` is a stored value but is never sent to the provider: every provider-facing layer (`chat_backend`, fallback chain, chat adapters, openai/responses providers) omits the `reasoning_effort` field when the resolved level is `none`.
- The model config page stores both fields on the provider record (`parameters.max_tokens` / `parameters.reasoning_effort`), like `context_window_tokens`; the page contract lives in `web-and-admin.md`「Model Config Page And Admin Contract」.
- `request_timeout_seconds`（配置页「请求超时时间(秒)」，位于最大输出TOKEN 右侧）是每次 provider attempt 的超时上限，同时约束外层 attempt 看门狗与流式首块/块间空闲超时：留空 = 未配置，回退全局默认 `DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS = 600`；配置则必须 `> 0`。解析优先级：调用方显式传入 → 该模型配置值 → 全局默认，链式回退因此按各模型自己的超时执行。取值真相源与其他参数一致：llm-config record `parameters.request_timeout_seconds`，binding payload 与 catalog 同步。配置里没有独立的 60/120 微默认——任何「未传超时」路径都指向同一个 600 默认。

If a provider reply looks truncated (for example a response ending at exactly the sent `max_tokens` with no tool call), check the node's history record first: 详见 `web-and-admin.md`「Node Detail Error History」.

## Model Retry And Key Rotation Config

每个模型绑定有 `retry_on`（关键词列表）与 `retry_count`（可重试错误的退避重试轮预算）。重试/轮换/退避的**行为契约**见 `runtime-overview.md`「Chat provider 超时与重试边界」；这里只讲配置语义。

- `retry_on` 是**真开关**，区分"未设置"与"显式置空"：省略该字段 → 用默认关键字 `["network","429"]`；显式设为 `[]`/`""` → 无关键字 → 任何错误都不触发退避重试。schema validator（`_normalize_retry_on`）与 `model_manager` 都按此区分，不再把显式空值回填成默认。输入按空格/逗号/换行等价分隔解析（配置页以「自动重试错误关键词(空格间隔)」收集）；关键词是单 token，含空白的短语会被拆开。
- 关键词命中的错误走**该模型的退避重试轮**：一轮 = 完整轮过该模型所有 key，轮间指数退避加抖动；命中即**不换 key**、也不零等待消费下游模型。轮预算耗尽才前进到链上下一个模型，全链耗尽报错停止（详见 `runtime-overview.md`「Chat provider 超时与重试边界」）。
- 请求体形状错误只按结构化 HTTP 状态判定（400/422），无文本关键字兜底；status 不可得的错误一律走正常轮换/降级判定。
- 换 key（轮换）只在错误**未命中 `retry_on`、且非请求体形状错误、且非内部运行时错误**时才发生，且为单趟：每个 key 各试一次即前进到链上下一个模型。**配置脚枪**：把 `401`/`403`/`invalid api key` 之类配进 `retry_on`，会让坏 key 被当成"可重试"从而只重试不换 key——坏 key 应靠"未命中 → 换 key"自愈，不要配进 `retry_on`。
- `retry_count`（配置页「重试次数」）是该模型可重试错误的最大退避重试轮数：0/未设置用内置默认 `DEFAULT_RETRYABLE_MODEL_ROUNDS=10`；非可重试错误的轮换恒为单趟、不受该值影响。同一个 key 配置在多个模型上互不影响——轮预算按（模型, key）槽位独立计，总请求上限 = Σ(每模型轮预算 × 该模型 key 数)。
- 负载均衡组内的成员**不继承** `retry_count`：组用 `models.loadBalanceGroups.<key>.maxRetryRounds`（配置里不填按 1；允许任意非负整数，0 与 1 等价——一轮都不重复就让位；只有负数报错）。原因是一个配了 `9999` 或 `9999999` 的成员会在组内永远不让位，组内平级 fallback 随之失效。`retry_count=0` 在这两条车道上含义不同，所以组预算必须写显式值，不能靠「省略字段」落到 10 轮默认。
- 管理面按**单 key 约束**引导配置：`org_graph_llm.js` 创建配置与保存连接信息时拒绝多 key 输入（逗号/换行分隔即报错），提示以「多配置组模型链」实现容量与容灾回退。运行时多 key 轮换代码路径保留以兼容历史存量配置；新配置从配置面即被限定为单 key。

## Frontdoor Context Window Contract

Frontdoor request-size control comes from the selected chat model's `context_window_tokens`.

- Every managed chat model and every `llm-config` chat binding must carry `context_window_tokens`.
- The value is runtime-authoritative: CEO/frontdoor resolves the currently selected model, reads its `context_window_tokens`, and uses that number for pre-send checks.
- For CEO/frontdoor, "runtime-authoritative" explicitly means the current live config revision from `get_runtime_config(...)`. Maintainers should not treat `loop.app_config` as an equivalent source of truth for send-time context-window decisions, because it may lag behind recent admin/model edits.
- There is no fallback to `loop.context_length` and no default floor.
- Inline legacy model payload migration must preserve `contextWindowTokens`; otherwise later `/api/models` reads and role-chain validation will misreport the model as missing a context window.

### Save-Time And Run-Time Validation

- Model create/update requires `context_window_tokens > 25000`.
- Role-chain batch save fails if any referenced model is missing a valid `context_window_tokens`.
  - The save path opportunistically backfills missing `models.catalog[].contextWindowTokens` from the bound `llm-config` record's `parameters.context_window_tokens` when possible (this mainly matters for older installs that upgraded after `context_window_tokens` became mandatory).
- Old stored models may still exist without that field, but if one is actually selected at runtime the turn fails fast instead of sending with an implicit unlimited window.

### What To Check When It Breaks

If an operator reports frontdoor send failures after a model or chain change, check in this order:

1. The selected model binding in `/api/models` really exposes `context_window_tokens`.
2. The saved `llm-config` record under `.g3ku/llm-config/records/*.json` kept the field during migration.
3. The role chain only references models that have a valid window configured.
4. The actual provider request estimate crossed the model's window.

Behavior once the estimate crosses the window: 详见 `runtime-overview.md`「Frontdoor Context Compression (Current Contract)」.

## Memory Runtime Settings Anchor

`tools/memory_runtime/resource.yaml` is the runtime settings anchor for long-term memory. It holds the Markdown notebook, durable queue, and memory-agent settings.

Settings surface:

- `document.*` controls the Markdown notebook layout, including `memory/MEMORY.md`, `memory/notes/`, the summary character limit, and the full document character ceiling.
  - The default `document.summary_max_chars` is `300`.
  - `document.compress_trigger_chars` and `document.compress_target_chars` define the post-commit snapshot compaction thresholds.
- `queue.*` controls the single durable queue and its failure parking store, including `memory/queue.jsonl`, `memory/ops.jsonl`, `memory/failed.jsonl`, batch size, max wait time, and the ordinary-turn review window size. `queue.review_interval_turns` is the per-session ordinary-turn review window size, defaulting to `5`. `queue.auto_requeue_on_success` (default `true`) enables the success-signal auto requeue: each applied batch requeues the oldest parked `provider_error` record at the queue tail; protocol-violation records never auto-requeue. Parking/requeue semantics live in `runtime-overview.md`「队列状态机与失败停车语义」.
- `agent.*` controls the dedicated memory-maintenance worker behavior.
  - `agent.repair_attempt_limit` is the number of repair retries after the first validation failure; total model attempts per batch = `1 + repair_attempt_limit` (default `2`, i.e. 3 attempts).
  - When every attempt still fails validation, the runtime falls back to a best-effort "minimal compression" write (uses each item's `minimal_memory` as the stored summary); the applied history row records `fallback: "minimal_compression"`. If that fallback is not possible (or the provider fails), the batch parks into `memory/failed.jsonl` instead of being terminally discarded.
- `mode`, `backend`, `bootstrap_mode`, and `compat.dual_write_legacy_files` are not part of the active memory runtime settings surface.

Project-config side keys:

- `models.roles.memory` is the dedicated model chain for the internal memory agent.
- `agents.roleIterations.memory` controls the memory agent's model-call round cap.
- `agents.roleConcurrency.memory` is fixed to `1`; it is persisted for config/UI symmetry but is not an operator-tunable parallelism knob.
- `models.roles.memory` may be empty, but when it is non-empty every referenced binding must have `capability=chat`. The admin route rejects non-chat bindings for the memory role.
- Unlike `ceo`, `execution`, and `inspection`, the `memory` role is allowed to be empty; that does not fail config load.
- If the queue head is already inside `processing` when an operator changes `models.roles.memory`, the already dispatched provider call is not hot-swapped. Before the next internal memory repair attempt, the runtime re-reads the latest revision and re-resolves the memory model chain.

The active internal memory writer prompt is the file-backed runtime asset `main/prompts/memory_agent.md`.

When debugging "memory queue stuck" reports, check both layers in order:

1. `models.roles.memory` / `agents.roleIterations.memory` in `.g3ku/config.json`
2. `tools/memory_runtime/resource.yaml` queue/document limits

Do not assume a valid CEO model chain implies a valid memory-agent chain; the memory worker does not fall back to CEO.

Queue execution and writer workflow: 详见 `operations-and-maintenance.md`「Memory Queue Workflow」.
