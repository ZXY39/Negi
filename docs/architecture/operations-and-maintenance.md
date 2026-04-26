# G3KU 运维与维护建议

本文档从“接手项目后怎么跑、怎么验证、怎么排障”的角度总结维护要点。

如果问题与 prompt cache 命中、跨 turn 上下文连续性、append-only request growth、或 actual request artifact 对账有关，请同时阅读：

- `runtime-overview.md`
- `context-and-cache-troubleshooting.md`

## 1. 基本启动方式

### 首选一键启动脚本

- Windows PowerShell: `.\start-g3ku.ps1`
- Linux / macOS: `./start-g3ku.sh`

适合：

- 普通用户快速启动项目
- 本地单机直接拉起 Web 与托管 worker
- 让脚本自动处理 `.venv`、依赖安装、基础配置兜底与已有托管进程重启

维护上要记住：

- 这两个脚本现在都是普通用户首选入口
- 它们会在启动前默认清理当前仓库下已有的 g3ku web / worker 进程
- 它们最终仍然是调用 `g3ku` bootstrap，再进入 `g3ku web`
- 当脚本使用 reload 模式时，Web 侧自动托管 worker 会关闭；这时要单独运行 `g3ku worker`

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
4. 先用 `.\start-g3ku.ps1`（Windows）或 `./start-g3ku.sh`（Linux / macOS）启动项目
5. 确认 Web API、任务面板、模型配置页能正常返回
6. 如项目启用中国渠道，再跑 `g3ku china-bridge doctor`

如果你要拆开验证启动链路，或排查“是脚本包装层问题还是 Web/runtime 本身问题”，再回退到手动运行 `g3ku web` / `g3ku worker`。

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
  For the queued Markdown memory runtime, inspect `memory/memory_state.sqlite3`, `memory/MEMORY.md`, `memory/notes/`, `memory/queue.jsonl`, and `memory/ops.jsonl` first. Dense store or checkpoint files are now secondary catalog/runtime projections rather than the primary long-term memory source.
  The queue is now model-driven: a stuck queue head usually means the dedicated memory agent route, provider call, or runtime validation is failing, not that a synchronous tool write failed inline.
  `memory/checkpoints.sqlite3` is now treated as a bounded short-term cache rather than an append-forever archive. The runtime trims older checkpoint rows per `(thread_id, checkpoint_ns)` automatically and CEO session deletion also purges that session key from the checkpointer. If file size stays high after deletes, inspect active threads and whether WAL truncation actually ran before assuming transcript cleanup failed.
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

如果问题是“一次性 cron / 定时提醒为什么没有真正创建或没有后续触发”，还要先区分两类情况：

- job 已成功落进 `.g3ku/cron/jobs.json`，但后续没有被消费：再继续查 scheduler / Web 主进程 / session dispatch
- `cron add` 本身在创建阶段就失败：尤其是 `at` 单次提醒，如果真正执行 `add_job()` 时目标时间已经过去，服务现在会直接拒绝创建并提示 `任务定时已过期，当前时间为<service-local time>，请立即执行或视情况废弃而不要创建过期任务`；这时应优先排查前门/tool 调用延迟、重试、参数错误，而不是先怀疑 scheduler 没触发

Maintenance note for `task_append_notice` / task message distribution:

- If a task appears stuck in `barrier_requested`, `barrier_draining`, or `distributing`, inspect these together before blaming the model:
  - `task_message_distribution_epochs` rows for the task
  - `task_node_notifications` rows for the task
  - `task_runtime_meta.distribution`
  - the current runtime frames for the barrier / frontier nodes
  - epoch payload fields such as `barrier_node_ids`, `drain_pending_node_ids`, `materialize_pending_entries`, `decision_records`, and `debug_trace`
  - the target node's `append_notice_context` metadata when the symptom is “notice vanished after compaction/compression”
- `decision_records` now separate delivered children from explicit skipped-child decisions. A frontier node with live children must account for every live child: delivered targets appear in `delivered_child_ids`, while non-delivered targets should appear in `skipped_child_decisions` with a reason. If a distribution turn fails with `distribution_decision_missing_child_decisions`, `distribution_decision_missing_should_distribute`, `distribution_decision_missing_reason`, or `distribution_decision_missing_message`, inspect the provider response/tool-call payload before treating the epoch as complete.
- `debug_trace` is the fastest historical entrypoint when a frontier node looks like it “never distributed”. The trace should tell you whether the control turn reached send preflight, which tool-call names came back from the provider, and whether validation failed before any child delivery was persisted.
- If `runtime_meta.distribution.state == "distributing"` and `frontier_node_ids` is still non-empty long after the previous frontier finished, check whether the runtime actually re-enqueued the task after writing `next_frontier_node_ids`. A successful root control turn plus durable child mailbox rows is not enough by itself; the next frontier still needs a fresh scheduler wakeup.
- If a parent node clearly consumed a distribution notice but spawn review still blocks children as if the old requirement were active, inspect that node's `append_notice_context.notice_records` and the spawn-review request payload. Spawn review now receives those consumed notices as `consumed_distribution_notices`, and the latest consumed notice should outrank stale `user_request` / `core_requirement` / `root_prompt` wording during review.
- `barrier_requested` means the append-notice transaction has captured the live tree and requested the task-wide barrier, but the tree has not started draining yet.
- `barrier_draining` means some live node is still outside a safe boundary. Check whether that node is waiting on model/tool IO or whether a runtime-frame phase is unexpectedly stuck.
- If `barrier_draining` persists while every known node frame already looks safe, inspect `materialize_pending_entries` in the epoch payload. That means the runtime is intentionally waiting for an active spawn round to finish creating child nodes before it snapshots the next distribution frontier. A non-empty `child_node_id` is no longer enough by itself; also verify that the child row exists and that its spawn-owner metadata matches the parent round entry.
- If a parent appears to have spawned duplicate child rows for one spawn entry, inspect the child metadata owner tuple: `spawn_owner_parent_node_id`, `spawn_owner_round_id`, and `spawn_owner_entry_index`. Runtime reconcile should bind one canonical child into `spawn_operations.entries[*].child_node_id` and mark the other rows with `duplicate_spawn_child=true`; marked duplicates are intentionally excluded from live distribution and should not keep `barrier_draining` alive.
- `distributing` means the active frontier is currently being processed through the ordinary task/node dispatcher. This is still queue-controlled node work, not a sidecar lane.
- After the distribution epoch reaches `completed`, global distribution should no longer appear as active. `runtime_meta.distribution.state`, `mode`, and `active_epoch_id` should be empty even when `pending_notice_node_ids` or `pending_mailbox_count` remain non-empty. Those remaining fields are node-local pending-notice bookkeeping only.
- If the frontend still paints the whole tree yellow after epoch completion, first check whether it is incorrectly treating non-empty `pending_notice_node_ids` or `pending_mailbox_count` as active global distribution. Only `barrier_requested`, `barrier_draining`, and `distributing` should drive whole-tree yellow distribution styling.
- If the frontend still shows `接收到新消息，等待节点处理` after a node already exposes the delivered message as pending in its message list / `pending_notice_count`, debug the browser-side handoff logic first. That sticky notice should clear once delivery is visible at the node level; later consumption into ordinary prompt context is a separate state.
- If root remains in `waiting_children` while `runtime_meta.distribution.mode == "task_wide_barrier"` still blocks that node, treat that as a barrier-priority regression before changing prompt logic.
- If a parent shows pending local notice metadata but continues running child work, that can now be expected. Check the node metadata `pending_notice_state.resume_mode`:
  - `ordinary` means the next resumed node turn may consume the notice immediately.
  - `wait_for_children` means the notice is durable but intentionally held out of the prompt until the parent no longer owns an incomplete child round.
- The same `pending_notice_state.resume_mode` contract now applies to child mailbox deliveries as well; child nodes are no longer allowed to bypass the `wait_for_children` boundary just because the notice arrived through `task_node_notifications`.
- If a node is listed in `pending_notice_node_ids` after the epoch completed, inspect `pending_notice_state.resume_mode` before calling it a regression:
  - `ordinary` means interrupted-turn recovery such as `pending_tool_turn` or `waiting_children` replay should still stay blocked until the pending local notice or mailbox delivery is consumed.
  - `wait_for_children` means `waiting_children` replay is allowed to continue the existing child round, but that recovery pass must still stop before the next ordinary model turn. The held notice must stay out of the ordinary prompt and must not open a new spawn round yet.
- If operators report `distribution finished but the parent stayed waiting`, inspect both:
  - node metadata `pending_notice_state`
  - parent `spawn_operations` / runtime frame `phase=waiting_children`
  That combination now expresses the strong parent-waiting constraint, not a stuck distribution loop.
- If a child execution node was previously terminal and now appears active again after append-notice, check whether its old acceptance node was logically invalidated rather than deleted. The acceptance record should still exist for audit, but it should no longer be authoritative for the current spawn entry.
- If operators report that a child "disappeared" from the browser tree right after successful execution, first distinguish which projection is wrong:
  - distribution recipient projection (`parent_visible`, handshake-driven recipient logic), or
  - browser tree visibility projection (`tree_visible`, acceptance display phase, distribution-active force-show behavior)
- Browser tree execution nodes should now remain visible in every status. If one disappears, treat that as a tree-visibility regression rather than expected `waiting_acceptance` behavior.
- If a parent can still message an acceptance node that has not started checking yet, that is expected. Distribution may merge new requirements into an inactive acceptance node's context before activation; inspect the acceptance node's message list / mailbox rows before treating that as premature execution.
- If an acceptance loop looks stuck after a rejection, inspect execution-node `metadata.acceptance_handshake` together with task-level `final_acceptance`. The expected sequence is still `waiting_acceptance -> waiting_execution_retry -> accepted|rejected_terminal`, but the default budget is now three total rejection outcomes: the first two rejections should return to `waiting_execution_retry`, and only the third should settle as `rejected_terminal`. Execution failure during any retry should end as `canceled_by_execution_failure`, not as another acceptance pass.
- If a notice appears in mailbox rows but not in the next model request, inspect whether it is still in the raw notice window or has already been rolled into a compressed notice-tail segment. Those tail blocks are now intentionally kept ahead of stage archive blocks in prompt assembly.
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

Provider retry troubleshooting note:

- 节点 `react_loop` 与 CEO/frontdoor `call_model` 的外层 provider-exhaustion 自动重试现在都是有限次：在底层 model/key/fallback chain 已经 exhausted 之后，当前 round 最多再做 3 次外层重试。
- 因此如果你看到 `Error calling Responses API` 连续刷屏，但任务/会话迟迟不结束，不要先假设“它还在无限自动重试”。先确认这些日志是否真的属于同一个 task/session。
- 当前 task 如果已经落到 `is_paused=true` / `pause_requested=true`，那说明另一个控制动作已经介入了；这和 provider retry 本身是两条不同的因果链。排查时应同时看 `task_commands` 是否出现 `pause_task`，而不是只盯着 provider 日志。

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
- 如果症状是执行节点/验收节点说“当前没有 candidate skills”或把本应当作 skill 的东西误判成“缺失的 callable 联网工具”，优先同时检查节点 runtime frame 与 `runtime-frame-messages:{node_id}` artifact 里的两组字段：
  - `contract_visible_skill_ids`：回答 `runtime_service._node_context_selection_inputs()` 当轮实际看到了哪些 contract-visible skills
  - `candidate_skill_ids`：回答 selector 最终留下了哪些 canonical skill candidates
- 如果还需要继续往下拆，再看 `skill_visibility_diagnostics`：
  - `registry_skill_ids`：回答 live `resource_registry` 当轮到底列出了哪些 skill
  - `entries[*].allowed_for_actor_role`：回答是不是在 `allowed_roles` 这一层就被挡住了
  - `entries[*].policy_effect`：回答 role policy 当轮给到的效果是不是 `allow`
  - `entries[*].included_in_contract_visible`：回答该 skill 最终有没有进入 `contract_visible_skill_ids`
- 排障顺序应先分层：
  - `contract_visible_skill_ids=[]`：优先怀疑 RBAC / governance / resource visibility 输入层
  - `contract_visible_skill_ids` 非空但 `candidate_skill_ids=[]`：优先怀疑 selector、dense fallback、或 contract/frame 重建链路
  - 两者都非空但模型文本里的前门 contract 已经把 `candidate_skills` 渲染成 `none`（无论文案是旧的 `candidate_skills: none`，还是新的 loadable 提示版本）：优先怀疑动态 contract 重建或 stale frame/消息恢复问题

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

### Provider Bundle Refresh

- `provider_tool_names` / provider-facing `tools[]` now track the current RBAC-visible concrete tool set for that path, not an old active-plus-pending exposure state machine.
- Ordinary turns may refresh the bundle immediately when membership changed. If the recomputed bundle has the same names in a different order, keep the persisted order exactly as-is to protect prompt-cache stability.
- A send that is already performing `token_compression` must keep the pre-existing provider bundle for that send. The first post-compression ordinary turn is the refresh point.
- `pending_provider_tool_names` and `provider_tool_exposure_commit_reason` are compatibility fields only on new writes. Expect `[]` and `""`, not a live commit workflow.
- Provider-facing schemas can now be broader than the callable contract for the round. That does not bypass runtime authorization: `tool_names` / callable contract still decides what the model is allowed to invoke this turn.

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

1. Inspect `memory/memory_state.sqlite3` for the authoritative memory rows, `refresh_count`, `passed_count`, `is_compressed`, and `from_user` state.
2. Inspect `memory/MEMORY.md` for the regenerated prompt snapshot currently injected into CEO/frontdoor.
3. Inspect `memory/queue.jsonl` for pending `write` / `delete` requests.
4. Inspect `memory/ops.jsonl` for the latest 7-day terminal batch history, including both applied rows and durable discarded rows plus final compression metadata.
5. If a processed row exposes `request_artifact_paths`, inspect the referenced files under `.g3ku/memory-requests/` before blaming prompt assembly or the provider adapter.
6. Use `g3ku memory current`, `g3ku memory queue`, and `g3ku memory flush` when you need a quick operator view without manually opening files.
7. If the queue head is stuck in `processing`, inspect `.g3ku/config.json -> models.roles.memory` before debugging the frontend or the catalog bridge.

The important maintenance boundary is:

- queue files are runtime metadata, not user memory content
- `memory_state.sqlite3` is the authoritative long-term memory state; `MEMORY.md` is the regenerated prompt snapshot
- `notes/` contains optional detail bodies and should stay small and human-readable
- `review_state.json` is per-session buffering metadata for the 5-turn ordinary review window; it is not committed user memory
- `ops.jsonl` is rolling terminal history, not an in-flight retry log or append-forever archive: applied rows and durable discarded outcomes belong there, queue-head error fields remain authoritative for engineering failures that are still retryable, and rows older than 7 days are pruned automatically during normal runtime reads/writes
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
- `delete`: natural-language memory deletion request waiting for the internal memory agent to resolve it to concrete ids

## Docker / Compose Startup

G3KU now has two supported operator startup modes:

- direct local startup through `start-g3ku.ps1` / `start-g3ku.sh`
- container startup through `compose.yaml`

For the container path, the maintenance contract is:

- the `web` container owns Web shell startup, heartbeat, cron, and China bridge supervision
- the `worker` container owns the background task worker only
- both containers must share the same workspace state

The required durable paths are:

- `.g3ku/`
- `memory/`
- `sessions/`
- `temp/`
- `skills/`
- `tools/`
- `externaltools/`

Do not treat only `.g3ku/` as sufficient persistence. Detached task temp files now live under `temp/tasks/`, external tool installs live under `externaltools/`, and mutable skill/tool resource copies may also need to survive restart.

Deployment unlock now has an operator-facing env contract:

- `G3KU_BOOTSTRAP_PASSWORD` allows a locked project to auto-unlock at process start
- `G3KU_INTERNAL_CALLBACK_URL` allows the worker container to call back into the web container over the Compose network instead of assuming `127.0.0.1`
- The official container image now also pins text/runtime locale explicitly with `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`, and `PYTHONIOENCODING=utf-8`. If container-only `exec`, validation-command, or Python traceback output shows mojibake, verify those three env vars before blaming prompt assembly or websocket rendering.

If Docker startup appears healthy but detached tasks never report back, inspect these in order:

1. the shared `.g3ku/internal-callback.json` payload
2. the effective `G3KU_INTERNAL_CALLBACK_URL` in both containers
3. whether `web` is healthy at `/api/bootstrap/status`
4. whether the worker container actually reached unlocked state

## 10. Memory Reset Workflow

`memory/` now contains both user long-term memory data and unified-context retrieval state, including tool/skill catalog retrieval indexes. A full physical reset of the directory removes all of these together.

Operator expectations:

- Use the explicit memory maintenance command to fully reset `memory/`; do not manually delete a subset of files.
- The reset recreates baseline managed files and sync state, but it does not immediately rebuild tool/skill catalog retrieval inside the command itself.
- After reset, user long-term memory is empty.
- After reset, tool/skill semantic retrieval is also empty until the next runtime startup.
- On startup, the runtime should rebuild catalog retrieval automatically by syncing the resource catalog back into the unified context store.

If tool/skill retrieval does not return after restart, first inspect resource runtime initialization and then confirm that the memory runtime reaches a healthy catalog-bridge state.
