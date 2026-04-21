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

还要额外记住一个新的运行时边界：

- 配置刷新仍然不会把一个“已经发出去的单次 provider 请求”中途热切换到新模型。
- 但对于已经进入 provider-failure retry 或 empty-response retry 的当前轮次，CEO/frontdoor 与节点运行时现在都会在下一次重试前检查 runtime revision。
- Memory queue 内部的 memory agent 现在也遵循同样的 revision 边界，但它看的不是 CEO/node 的 provider retry，而是 memory 自己的修复边界：`assess -> apply` 交接点，以及同一批次里的 validation/repair 重试点。
- 如果 revision 已变化，旧模型链的重试会失效，当前轮会用新的 model refs 重新开始，而不是继续无限重试旧链。

维护上要把这理解成“重试边界上的重建”，而不是“请求中途热切模型”。如果用户反馈“改完模型链后旧重试还在跑”，重点检查：

1. 对应进程是否真的执行到了 runtime refresh
2. 当前问题是否发生在 retry 边界，而不是单次 still-in-flight 的 provider request 内
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
- memory embedding / rerank 绑定
- 导出 runtime target
- 把 secrets 存进安全 overlay，而不是明文长期放在 record 中

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

支持的 canonical ids：

- `qqbot`
- `dingtalk`
- `wecom`
- `wecom-app`
- `wecom-kf`
- `wechat-mp`
- `feishu-china`

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

### memory embedding / rerank 不生效

先看：

- `.g3ku/llm-config/memory_binding.json`
- `LLMConfigFacade.get_memory_binding()`

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

## Image Multimodal Binding Flag

`models.catalog[]` now carries a second binding-owned chat field: `image_multimodal_enabled` (`imageMultimodalEnabled` in saved JSON / admin payload aliases).

- Default value is `false`.
- Existing saved models that do not have the field must be treated as `false` at load time; there is no backfill migration that rewrites old configs just to add the default.
- The flag belongs to the managed model binding layer, not to the provider config record. It must persist in `.g3ku/config.json` under `models.catalog[]`, and it must not be written into `.g3ku/llm-config/records/*.json`.
- `/api/models` and `/api/llm/bindings` both expose and update this field because they are two views over the same binding-owned metadata.
- The practical runtime meaning is intentionally narrow: Web CEO image uploads only expand into provider-visible multimodal request blocks when the currently selected chat binding has `image_multimodal_enabled=true`.
- When the flag is `true` and the current turn actually uploads images, CEO/frontdoor also changes the provider-visible attachment note for that turn: the model sees a direct-visual guidance note rather than the old local-path attachment text. Attachment metadata and transcript/debugging storage still remain available outside the provider-visible prompt.

## Frontdoor Context Window Contract

Frontdoor request-size control now comes from the selected chat model's `context_window_tokens`, not from a separate frontdoor summary tuning surface.

- Every managed chat model and every `llm-config` chat binding must carry `context_window_tokens`.
- The value is runtime-authoritative: CEO/frontdoor resolves the currently selected model, reads its `context_window_tokens`, and uses that number for pre-send checks.
- There is no fallback to `loop.context_length`, no default floor, and no legacy global-summary threshold override.
- Inline legacy model payload migration must preserve `contextWindowTokens`; otherwise later `/api/models` reads and role-chain validation will misreport the model as missing a context window.

### Save-Time And Run-Time Validation

- Model create/update now requires `context_window_tokens > 25000`.
- Role-chain batch save fails if any referenced model is missing a valid `context_window_tokens`.
  - The save path will now opportunistically backfill missing `models.catalog[].contextWindowTokens` from the bound `llm-config` record's `parameters.context_window_tokens` when possible (this mainly matters for older installs that upgraded after `context_window_tokens` became mandatory).
- Old stored models may still exist without that field, but if one is actually selected at runtime the turn now fails fast instead of sending with an implicit unlimited window.

### What To Check When It Breaks

If an operator reports frontdoor send failures after a model or chain change, check in this order:

1. The selected model binding in `/api/models` really exposes `context_window_tokens`.
2. The saved `llm-config` record under `.g3ku/llm-config/records/*.json` kept the field during migration.
3. The role chain only references models that have a valid window configured.
4. The actual provider request estimate crossed the model's window, in which case frontdoor now stops before send instead of trying a legacy semantic-summary path.

## Memory Runtime Settings Anchor

`tools/memory_runtime/resource.yaml` is still the runtime settings anchor for long-term memory, but the meaning of that settings surface changed.

- `document.*` now controls the Markdown notebook layout, including `memory/MEMORY.md`, `memory/notes/`, the summary character limit, and the full document character ceiling.
  - The current default `document.summary_max_chars` is `250`. When a memory candidate cannot be expressed within that one-line limit, the intended writer behavior is to compress it or switch to a one-line summary plus `note` pattern rather than overflowing the notebook line.
- `queue.*` now controls the single durable queue, including `memory/queue.jsonl`, `memory/ops.jsonl`, batch size, max wait time, and the ordinary-turn review window size.
- `agent.*` now controls the dedicated memory-maintenance worker behavior.
- Older `store.*`, `retrieval.*`, and `embedding.*` sections still matter for the catalog bridge because tool/skill semantic narrowing still relies on that catalog-only projection, even though long-term memory正文 no longer depends on the old `rag_memory` runtime.
- The earlier transition fields such as `mode`, `backend`, `bootstrap_mode`, and `compat.dual_write_legacy_files` are no longer part of the active memory runtime settings surface.

There is now also a project-config side contract that maintainers must keep straight:

- `models.roles.memory` is the dedicated model chain for the internal memory agent.
- `agents.roleIterations.memory` controls the memory agent's model-call round cap.
- `agents.roleConcurrency.memory` is fixed to `1`; it is persisted for config/UI symmetry but is not an operator-tunable parallelism knob.
- `models.roles.memory` may be empty, but when it is non-empty every referenced binding must have `capability=chat`. The admin route now rejects embedding/rerank or other non-chat bindings for the memory role instead of letting the queue fail later at runtime.
- Unlike `ceo`, `execution`, and `inspection`, the `memory` role is allowed to be empty. That does not fail config load; instead it blocks the memory queue head at runtime until the role is configured.
- If the queue head is already inside `processing` when an operator changes `models.roles.memory`, the already dispatched provider call is not hot-swapped. But before the next internal memory repair attempt, the runtime now re-reads the latest revision and re-resolves the memory model chain, so post-refresh repair rounds do not stay pinned to the old route forever.

Maintainers should no longer read `tools/memory_runtime/resource.yaml` as "structured memory database tuning only". It now mixes two boundaries:

- Markdown long-term memory notebook settings
- Catalog bridge retrieval settings

When debugging "memory queue stuck" reports, check both layers in order:

1. `models.roles.memory` / `agents.roleIterations.memory` in `.g3ku/config.json`
2. `tools/memory_runtime/resource.yaml` queue/document limits

Two specific memory-runtime config semantics changed again:

- `queue.review_interval_turns` now means the per-session ordinary-turn review window size, defaulting to `5`, not the earlier hashed sampling interval.
- The internal memory prompts are now file-backed runtime assets under `main/prompts/memory_agent.md` and `main/prompts/memory_assessor.md`. If memory processing behavior looks wrong after prompt edits, inspect those files before changing Python code.
- The current writer prompt contract is intentionally stricter than the original rollout:
  - admission is narrowed to durable dissatisfaction signals, reusable user suggestions, explicit remember requests, and repeated-mistake lessons
  - memory summaries should read like `condition + requirement`
  - semantic duplicates must prefer `rewrite` and must not be re-added as synonymous `adds`
  - write batches may now also resolve through an explicit `noop_reason` path when the candidate should be ignored and the durable notebook should remain unchanged; this path is write-only and must not be combined with add/rewrite/delete/note changes

Do not assume a valid CEO model chain implies a valid memory-agent chain. The memory worker no longer falls back to CEO.
