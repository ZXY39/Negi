# G3KU 运维与维护建议

本文档从“接手项目后怎么跑、怎么验证、怎么排障”的角度总结维护要点。

如果问题与 prompt cache 命中、跨 turn 上下文连续性、append-only request growth、或 actual request artifact 对账有关，请同时阅读：

- `runtime-overview.md`
- `context-and-cache-troubleshooting.md`

## 1. 基本启动方式

### CLI

- `g3ku agent -m "Hello"`

适合验证：

- 配置是否正确
- CEO 模型是否可用
- 同步会话路径是否正常

### Web

- `g3ku web`

适合验证：

- Web UI
- main task runtime
- heartbeat
- China bridge 自动拉起

### Worker

- `g3ku worker`

适合验证：

- 后台 task worker 路径
- Web/runtime 分离执行场景

## 2. 新接手后的第一轮检查

建议按下面顺序做环境确认：

1. 检查 `.g3ku/config.json`
2. 运行 `g3ku status`
3. 运行一次 `g3ku agent -m "test"`
4. 再启动 `g3ku web`
5. 确认 Web API、任务面板、模型配置页能正常返回
6. 如项目启用中国渠道，再跑 `g3ku china-bridge doctor`

当前 `g3ku status` 的记忆区块应按 queued Markdown runtime 理解：

- 它会显示 `Memory Notebook`、`Memory Notes Dir`、`Memory Queue`、`Memory Ops Log`、`Memory Checkpointer`
- 它不再把 `Memory Mode`、`Memory Store(SQLite)`、`Memory Store(Qdrant)`、`pending_facts.jsonl`、`audit.jsonl` 当作当前长期记忆健康指标

## 3. 关键状态文件与目录

日常排障时，先熟悉这些目录：

- `.g3ku/config.json`
  项目配置

- `.g3ku/llm-config/`
  模型配置仓库与 memory binding

- `.g3ku/main-runtime/`
  任务运行时 SQLite、artifacts、event history

  通过 `g3ku web` 启动并启用 auto worker 时，这里还应重点关注两份日志：

  - `.g3ku/main-runtime/manual-web-run.log`
    Web 主进程日志
  - `.g3ku/main-runtime/managed-worker.log`
    自动拉起的后台 task worker 日志

- `.g3ku/china-bridge/`
  China bridge 状态与日志

- `.g3ku/web-ceo-continuity/`
  completed Web CEO session continuity sidecars。重启后继续 completed session、manual pause terminal stop、以及异常中断后的最新 authoritative frontdoor baseline 恢复都先看这里。

- `memory/`
  Maintenance note:
  For the queued Markdown memory runtime, inspect `memory/MEMORY.md`, `memory/notes/`, `memory/queue.jsonl`, and `memory/ops.jsonl` first. Dense store or checkpoint files are now secondary catalog/runtime projections rather than the primary long-term memory source.
  The queue is now model-driven: a stuck queue head usually means the dedicated memory agent route, provider call, or runtime validation is failing, not that a synchronous tool write failed inline.
  记忆相关文件、qdrant、checkpoint 等

- `sessions/`
  会话持久化数据

## 4. 测试结构

测试以 `tests/` 为主，很多测试按资源/运行时主题分布在：

- `tests/resources/`

从现有命名看，测试重点覆盖：

- main runtime
- CEO/frontdoor
- tool registry 与 hydration
- web runtime
- heartbeat prompt lane
- China bridge
- memory runtime

## 5. 推荐的排障顺序

### 会话无回复

先看：

- `g3ku/runtime/session_agent.py`
- `g3ku/runtime/bridge.py`
- Web 场景下再看 `g3ku/heartbeat/session_service.py`

### 任务没创建或没推进

先看：

- `main/service/runtime_service.py`
- `main/runtime/node_runner.py`

Maintenance note for `task_append_notice` / task message distribution:

- If a task appears stuck in `pause_requested` or `distributing`, inspect these together before blaming the model:
  - `task_message_distribution_epochs` rows for the task
  - `task_node_notifications` rows for the task
  - `task_runtime_meta.distribution`
  - the current runtime frames for the frontier nodes
- `pause_requested` means the append-notice transaction has requested the existing pause barrier but ordinary execution has not yet consumed the next distribution step.
- `distributing` means the active frontier is currently being processed through the ordinary task/node dispatcher. This is still queue-controlled node work, not a sidecar lane.
- After the final frontier finishes, v1 clears distribution state and schedules the next ordinary task run instead of keeping a long-lived `resuming` marker in runtime meta.
- If a child execution node was previously terminal and now appears active again after append-notice, check whether its old acceptance node was logically invalidated rather than deleted. The acceptance record should still exist for audit, but it should no longer be authoritative for the current spawn entry.
- If force delete is requested during message distribution, deletion should win immediately. Do not wait for the epoch to finish naturally; confirm instead that epochs/mailboxes were cancelled or purged before the task row disappeared.

如果任务已经创建，但表现为“响应明显变慢”“长时间停在 `model.chat.await_response`”或“前端只看到 task-event 在刷”，优先同时对照：

- `.g3ku/main-runtime/manual-web-run.log`
- `.g3ku/main-runtime/managed-worker.log`

其中 worker 日志更关键，因为 provider 请求、SSE 诊断和模型超时通常发生在独立的 worker 进程里。

排查时优先搜索：

- `responses stream diagnostics`
- `openai_codex stream diagnostics`
- `Error calling Responses API`
- `model attempt timeout`

并结合以下超时语义判断“慢”是不是异常：

- 对 streaming-first chat provider（`ResponsesProvider`、`OpenAICodexProvider`、`CustomProvider`、`LiteLLMProvider`）：
  - 60s 内必须收到首个 chunk（任意 chunk）
  - 流开始后连续 60s 无新 chunk 触发超时
  - 不支持流式时，同次 attempt 内自动回退非流式，非流式用 120s 首响应超时
- 上述 provider 的外层包装不会再加单次 attempt 的 60s 硬总时长截断；只要 chunk 仍在持续到达，整体生成可超过 60s

日志里优先看这些字段：

- `first_chunk_received_ms`
- `first_text_delta_received_ms`
- `chunk_count`
- `last_chunk_kind`
- `stream_completed_ms` / `stream_failed_ms`

### 缓存命中下降或上下文疑似丢失

先看：

- `docs/architecture/context-and-cache-troubleshooting.md`
- `.g3ku/web-ceo-requests/`
- `.g3ku/web-ceo-continuity/`
- `sessions/`
- 相关的 paused / inflight snapshot

排障时不要只盯 `Input Tokens`。优先区分：

- `prompt_cache_key_hash` 是否 churn
- `actual_request_hash` 是否变化
- `provider_request_body.input` 的公共前缀是否提前断裂
- usage 记录是否能和本地 actual request artifact 对得上
- completed session 重启后是否恢复到了 continuity sidecar，而不是直接退回 transcript/history fallback

### 工具调用异常

先看：

- `g3ku/agent/tools/registry.py`
- `g3ku/runtime/context/`
- `main/service/runtime_service.py`

### 模型配置不生效

先看：

- `g3ku/config/loader.py`
- `g3ku/llm_config/facade.py`

如果问题表现为“前端模型配置页已经显示了新的 API key / 新的 key 数量，但 CEO 或 worker 还像在用旧 key”：

- 先确认对应 runtime refresh 是否真的执行到了目标进程
- Web 托管 worker 路径下，优先看 `task_commands` 里的 `refresh_runtime_config` 是否完成，以及 `.g3ku/main-runtime/managed-worker.log`
- 当前系统约定是：显式 runtime refresh 会同时重载已解锁进程里的 bootstrap security overlay 缓存；如果 refresh 没跑到，老进程可能继续拿旧 secret 快照
- 如果 refresh 已完成但行为仍不对，再考虑 worker 进程是否需要重启，或是否存在多个旧 worker / 旧 Web 进程残留

### 中国渠道异常

先看：

- `g3ku/china_bridge/supervisor.py`
- `g3ku/china_bridge/transport.py`
- `subsystems/china_channels_host/src/`

## 6. 维护时的高风险修改类型

### 修改 session turn 逻辑

涉及文件：

- `g3ku/runtime/session_agent.py`

风险：

- 容易同时影响 user turn、heartbeat turn、cron turn、pause/resume。

### 修改 task runtime 总装配

涉及文件：

- `main/service/runtime_service.py`

风险：

- 工具集、治理、日志、worker、内容服务可能一起被带坏。

### 修改工具可见性与候选池

涉及文件：

- `g3ku/runtime/context/`
- `main/service/runtime_service.py`

风险：

- 不一定会报错，但会显著改变 agent 行为，是高隐蔽性回归。

### 修改 China bridge 协议边界

涉及文件：

- `g3ku/china_bridge/*`
- `subsystems/china_channels_host/src/*`

风险：

- Python 和 Node 两侧都要同步验证。

## 7. 维护建议

### 先判断问题属于哪条主线

不要一上来全文搜索。先判断它属于：

- 会话主线
- 任务主线
- heartbeat
- Web/API
- 配置/模型
- China bridge

### 优先从集成点往下看

很多问题不在最底层，而是在集成处。例如：

- Web runtime 装配失败
- session key 路由错误
- candidate tool 没进 callable set

### 改提示词也要当成代码改动看待

如：

- `g3ku/runtime/prompts/heartbeat_rules.md`
- `g3ku/runtime/prompts/ceo_frontdoor.md`

这些文件会直接改变 agent 行为，回归风险不低。

### 对 `main/service/runtime_service.py` 保持敬畏

它过于 central。每次改动后，最好至少验证：

- 任务创建
- 节点执行
- task detail API
- 工具可见性

## 8. 新功能接入时的建议切入点

### 加新 tool

先看：

- `g3ku/agent/tools/`
- `tools/<tool_name>/`
- `main/service/runtime_service.py`

### 加新 skill

先看：

- `skills/`
- `g3ku/agent/skills.py`
- `ResourceManager` 相关逻辑

### 加新 China channel

先看：

- `subsystems/china_channels_host/channel_registry.json`
- Node vendor/native host 结构
- Python channel registry 与 bridge 配置

## 9. 最小验证清单

做完较大改动后，建议至少人工验证：

1. CLI 同步会话可用
2. Web 页面可打开
3. Web 会话可发送并返回
4. 至少一个异步任务可创建并完成
5. task detail / node detail API 正常
6. 若改动涉及工具系统，验证候选工具与 callable 工具行为
7. 若改动涉及 China bridge，跑 `g3ku china-bridge doctor`

## Memory Queue Workflow

For the queued Markdown memory runtime, the first operator checks should now be:

1. Inspect `memory/MEMORY.md` for the currently committed long-term memory snapshot.
2. Inspect `memory/queue.jsonl` for pending `write` / `delete` / `assess` requests.
3. Inspect `memory/ops.jsonl` for the last successfully applied batches.
4. Use `g3ku memory current`, `g3ku memory queue`, and `g3ku memory flush` when you need a quick operator view without manually opening files.
5. If the queue head is stuck in `processing`, inspect `.g3ku/config.json -> models.roles.memory` before debugging the frontend or the catalog bridge.

The important maintenance boundary is:

- queue files are runtime metadata, not user memory content
- `MEMORY.md` is the only committed long-term memory source
- `notes/` contains optional detail bodies and should stay small and human-readable
- `review_state.json` is per-session buffering metadata for the 5-turn ordinary review window; it is not committed user memory
- `ops.jsonl` is not a complete failure ledger anymore; queue-head error fields are authoritative for in-flight engineering failures
- only one live process should hold the memory-worker lease at a time; extra web/worker processes may exist, but they should leave queue consumption to the active lease holder

Operator debugging order for a stuck queue head:

1. Check whether `models.roles.memory` is empty or points to an invalid model chain.
2. Check the queue-head `last_error_text`, `last_error_at`, `retry_after`, and `processing_started_at`.
3. Check worker logs for provider/tool-call failures or semantic validation failures.
4. Only after the runtime side is healthy should you debug the web `记忆管理` page.

Operator debugging order for duplicated successful memory writes:

1. Compare `memory/ops.jsonl` rows by `request_id`, not only by `batch_id`.
2. If the same `request_id` appears in two successful processed rows, treat that as duplicate consumption of one queue request rather than proof that the frontdoor called `memory_write` twice.
3. Inspect live `python -m g3ku web` / `python -m g3ku worker` processes and verify only one runtime instance should be actively consuming the memory queue.
4. If multiple runtimes were active, treat duplicate rows as historical worker-contention evidence first, then inspect whether the current build already has the memory-worker lease and processed-request dedupe protections.

### Memory Maintenance CLI

The memory CLI now keeps only the queued Markdown runtime operator surface:

- `g3ku memory current`
- `g3ku memory queue`
- `g3ku memory flush`
- `g3ku memory doctor`
- `g3ku memory reconcile-notes`
- `g3ku memory import-legacy <path>`
- `g3ku memory cleanup-legacy`

The old legacy-only commands such as runtime stats/trace/explain, `migrate-v2`, `reset-runtime`, decay, and pending-fact review are no longer part of the active operator contract.

The operator-oriented maintenance commands beyond `current`, `queue`, and `flush` are:

- `g3ku memory doctor`
  Read-only health check for the queued Markdown memory layout.
- `g3ku memory reconcile-notes`
  Explicit note-ref reconciliation for `MEMORY.md` and `memory/notes/`.
- `g3ku memory import-legacy <path>`
  Minimal one-shot importer for legacy memory exports.

Use them with these boundaries in mind:

- `doctor` is inspection-only. It does not rewrite `MEMORY.md`, create notes, delete notes, or mutate queue state.
- `doctor` also avoids implicit runtime bootstrap writes. If `memory/` (or `MEMORY.md`, `queue.jsonl`, `ops.jsonl`, `notes/`) does not exist yet, the command still reports health based on current disk state and does not create those paths.
- `doctor` should be the first stop when an operator suspects notebook corruption or a blocked queue head. It checks:
  - whether `MEMORY.md` still matches the managed Markdown block format
  - whether every visible `ref:note_xxxx` points to an existing note file
  - whether orphan note files exist under `memory/notes/`
  - whether `memory/queue.jsonl` contains malformed JSON rows (reported with line-level diagnostics)
  - whether `queue.jsonl` currently has an old `processing` head that looks stuck
- If malformed queue rows are detected, `doctor` now reports them as explicit queue-parse issues and exits non-zero. It should not crash with a bare JSON decode exception.
- `reconcile-notes` is the explicit repair path for note/file consistency. It may create placeholder note files for missing refs, and it only deletes orphan note files when the operator passes the explicit delete flag.
- `import-legacy` is dry-run by default. The operator must pass `--apply` before it writes `MEMORY.md` or any note files.
- The default dry-run path for `import-legacy` is inspection-only as well: it parses the legacy payload and prints a summary without creating `memory/`, `MEMORY.md`, `queue.jsonl`, `ops.jsonl`, or note files.
- `import-legacy --apply` is intentionally conservative. The target memory notebook should already be empty, and the operator should not use it as a merge tool for a live non-empty queue.
- `cleanup-legacy` is also dry-run by default. It lists removable legacy artifacts such as `HISTORY.md`, structured projections, sync journals, pending/audit files, and `context_store/`.
- `cleanup-legacy --apply` refuses to delete data-bearing legacy artifacts while `MEMORY.md` is still empty. The intended order is to import or explicitly review old data first, then delete the leftovers once the new notebook already contains the migrated memory.

Recommended operator order:

1. Run `g3ku memory doctor` first.
2. If the only issues are missing note files or orphan notes, use `g3ku memory reconcile-notes`.
3. If the notebook is empty and you are doing a controlled migration, run `g3ku memory import-legacy <path>` once without `--apply`, inspect the summary, then rerun with `--apply`.
4. After migration is complete and `MEMORY.md` is already authoritative, run `g3ku memory cleanup-legacy` once in dry-run mode, review the paths, then rerun with `--apply` to remove leftovers.

Two queue-head recovery caveats matter during operations:

- A `processing` head that survives restart is expected durable state. Do not assume it means a live worker is still attached.
- If `retry_after` is still in the future, the restarted worker should leave that head untouched and keep later items blocked. Once `retry_after` has passed, the same head becomes eligible for retry.
- `processing_started_at` is the first-claim timestamp for that head batch. It should remain stable across retries, so a new `last_error_at` with an old `processing_started_at` is normal.

Operator interpretation of the three queue entry types:

- `write`: explicit or already-refined memory text waiting for real memory processing
- `delete`: id-based memory deletion waiting for real memory processing
- `assess`: buffered ordinary-turn/compression window waiting for the assessor lane; if the assessor returns `null`, no committed memory change follows

## 10. Memory Reset Workflow

`memory/` now contains both user long-term memory data and unified-context retrieval state, including tool/skill catalog retrieval indexes. A full physical reset of the directory removes all of these together.

Operator expectations:

- Use the explicit memory maintenance command to fully reset `memory/`; do not manually delete a subset of files.
- The reset recreates baseline managed files and sync state, but it does not immediately rebuild tool/skill catalog retrieval inside the command itself.
- After reset, user long-term memory is empty.
- After reset, tool/skill semantic retrieval is also empty until the next runtime startup.
- On startup, the runtime should rebuild catalog retrieval automatically by syncing the resource catalog back into the unified context store.

If tool/skill retrieval does not return after restart, first inspect resource runtime initialization and then confirm that the memory runtime reaches a healthy catalog-bridge state.
