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
  把模型键映射到 `ceo / execution / inspection`

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

定义任务运行时存储与调度参数。

### `china_bridge`

定义 Node 宿主、控制端口、自动启动、中国渠道配置。

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
- Main runtime 节点的模型链在任务运行时 chat 后端的每个链轮边界活解析（`model_refs_resolver` 读取当前角色链），路由/绑定变更在下一个链轮即生效，不必等下一个回合；可重试失败退避等待结束后同样先重新解析链再继续重试。若跨链轮边界时 runtime revision 已变化，重试循环抛出带 `config_revision_changed` 标记的可重试耗尽错误，react loop 以 `provider_retry_invalidation` 重建回合（重取链、重组请求）而不是继续用旧链消耗重试。memory queue 内部 agent 看的不是 CEO/node 的 provider retry，而是 memory 自己的同批次 validation/repair 重试点，普通 review window 不经过单独的 `assess -> apply` 交接。

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

绑定 key 是稳定主键：它唯一标识 `models.catalog[]` 条目，同时被 `models.roles.*` 和 `agents.multi_agent.orchestrator_model_key` 引用。管理面创建 binding 时以记录的 `default_model` 为基底自动生成 key，遇到同名模型时追加数字后缀去重，因此 key 不再等于模型名，不同供应商可以添加同名模型。模型名是记录里的 `default_model`（`provider:model` 里的 model 段），仅作为展示标题，编辑模型改变 `default_model` 时标题随之更新而不改写 key；命名与展示职责详见 `web-and-admin.md`「Model Config Page And Admin Contract」。

## 8. secret 的真实去向

配置里的 secret 不一定直接写回文件。

当前机制里：

- `config.json` 里会保留结构性配置
- 真正 secret 通过 bootstrap security overlay 管理
- `LLMConfigFacade` 存 record 时会清洗掉明文 secret，再把 secret 写入 overlay

所以：

- 如果你看到某个 config record 没有明文 api key，不代表配置丢了
- 排查模型鉴权问题时，不能只看 JSON 文件

## 9. China bridge 配置

中国渠道统一走：

- `chinaBridge.channels.<channel-id>`

而不是历史上的 `channels.*`。

loader 会显式拒绝 legacy `channels.*` 配置，这一点在迁移和排障时很重要。

支持的 canonical ids 详见 `china-channels.md`「支持的 canonical channel ids」。

单个渠道的行为字段挂在对应渠道记录下、由 Node 宿主消费，例如 `chinaBridge.channels.qqbot.progressMode`（过程里程碑消息，`off` / `milestones`）与 `replyFinalOnly`。注意宿主侧合并账号配置（`mergeQQBotAccountConfig`）不走 zod parse 的默认值补全路径，这类字段的默认值需要在消费代码里用 `??` 兜底。

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

### China bridge 配置改了但宿主行为没更新

先看：

- `build_runtime_config_payload(...)`
- `g3ku/shells/web.py` 中的 refresh / sync china bridge 逻辑

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
- `G3KU_BOOTSTRAP_MASTER_KEY` still exists, but it remains the internal fast path for a web-managed child worker rather than the preferred Compose/operator contract.

Maintainers should keep the persistence boundary straight:

- `.g3ku/config.json` remains the structural project config source of truth
- `.g3ku/llm-config/` still stores provider/binding records
- `.g3ku/secret-realms/` and `.g3ku/llm-config/master.key` still hold bootstrap secret state

So for container persistence, mounting only `memory/` or `sessions/` is not enough. A containerized project that must keep model bindings, secrets, and unlock state across restart must also persist `.g3ku/`.

If a worker container reports `project_locked` while the web container appears healthy, inspect:

1. whether both containers received the same `G3KU_BOOTSTRAP_PASSWORD`
2. whether the shared `.g3ku/` volume is actually the same volume
3. whether the existing project was already initialized with a bootstrap password rather than left in `setup` mode

## Image Multimodal Binding Flag

`models.catalog[]` carries a second binding-owned chat field: `image_multimodal_enabled` (`imageMultimodalEnabled` in saved JSON / admin payload aliases).

- Default value is `false`.
- Existing saved models that do not have the field must be treated as `false` at load time; there is no backfill migration that rewrites old configs just to add the default.
- The flag belongs to the managed model binding layer, not to the provider config record. It must persist in `.g3ku/config.json` under `models.catalog[]`, and it must not be written into `.g3ku/llm-config/records/*.json`.
- `/api/models` and `/api/llm/bindings` both expose and update this field because they are two views over the same binding-owned metadata.

Runtime gating of image uploads by this flag: 详见 `web-and-admin.md`「Image Upload Gating」.

## Model Request Parameter Defaults

Per-model generation parameters (`max_tokens`, `temperature`, `reasoning_effort`) are resolved from the llm-config record's `parameters` and applied to every provider request.

- `max_tokens` always resolves to an explicit value: the per-model `parameters.max_tokens` wins; when the record has none, the runtime falls back to the global default `DEFAULT_MAX_OUTPUT_TOKENS = 65536`. Requests therefore always carry an explicit output cap instead of inheriting the provider-side default (which can silently truncate long generations).
- The engine-global `agents.defaults.maxTokens` (default `65536`) is the CEO-loop fallback; main-runtime nodes resolve per model through `_resolve_model_request_parameters` first.
- `reasoning_effort` uses six managed levels: `none` (deep thinking disabled), `low`, `medium` (default), `high`, `xhigh`, `max`. Per-model `parameters.reasoning_effort` wins over the engine default.
- `none` is a stored value but is never sent to the provider: every provider-facing layer (`chat_backend`, fallback chain, chat adapters, openai/responses providers) omits the `reasoning_effort` field when the resolved level is `none`.
- The model config page stores both fields on the provider record (`parameters.max_tokens` / `parameters.reasoning_effort`), like `context_window_tokens`; the page contract lives in `web-and-admin.md`「Model Config Page And Admin Contract」.

If a provider reply looks truncated (for example a response ending at exactly the sent `max_tokens` with no tool call), check the node's history record first: 详见 `web-and-admin.md`「Node Detail Error History」.

## Model Retry And Key Rotation Config

每个模型绑定有 `retry_on`（关键词列表）与 `retry_count`（同模型重试轮数）。重试/轮换/退避上限的**行为契约**见 `runtime-overview.md`「Chat provider 超时与重试边界」；这里只讲配置语义。

- `retry_on` 是**真开关**，区分"未设置"与"显式置空"：省略该字段 → 用默认关键字 `["network","429"]`；显式设为 `[]`/`""` → 无关键字 → 任何错误都不触发整链重试。schema validator（`_normalize_retry_on`）与 `model_manager` 都按此区分，不再把显式空值回填成默认。
- 关键词命中的错误走整链退避重试（受 20 分钟退避累计上限约束），且命中即**不换 key**。
- 换 key（轮换）只在错误**未命中 `retry_on`、且非请求体形状错误（HTTP 400/422）、且非内部运行时错误**时才发生。**配置脚枪**：把 `401`/`403`/`invalid api key` 之类配进 `retry_on`，会让坏 key 被当成"可重试"从而只重试不换 key——坏 key 应靠"未命中 → 换 key"自愈，不要配进 `retry_on`。
- `retry_count` 是该模型同轮内的重试轮数，与 `api_key_count` 一起构成 key×轮 的尝试预算；它和轮换共用同一判据，故只对**会触发轮换**的错误（未命中 `retry_on` 且非 400/内部）生效，可重试错误改走整链退避重试。

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
- `queue.*` controls the single durable queue, including `memory/queue.jsonl`, `memory/ops.jsonl`, batch size, max wait time, and the ordinary-turn review window size. `queue.review_interval_turns` is the per-session ordinary-turn review window size, defaulting to `5`.
- `agent.*` controls the dedicated memory-maintenance worker behavior.
  - `agent.repair_attempt_limit` is the number of repair retries after the first validation failure; total model attempts per batch = `1 + repair_attempt_limit` (default `2`, i.e. 3 attempts).
  - When every attempt still fails validation, the runtime falls back to a best-effort "minimal compression" write (uses each item's `minimal_memory` as the stored summary) instead of discarding; the applied history row records `fallback: "minimal_compression"`.
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
