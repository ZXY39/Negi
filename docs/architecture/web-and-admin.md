# G3KU Web And Admin Architecture

This document describes how the web shell, admin APIs, and browser runtime fit together for day-to-day maintenance.

## Responsibilities And Boundaries

- `g3ku/shells/web.py` owns web runtime startup and binds the backend service into HTTP routes.
- `g3ku/web/frontend/*` owns browser-side rendering, interaction logic, and shell state presentation.
- `main/api/*` and `g3ku/runtime/api/*` own backend contracts consumed by the frontend.
- The browser shell should present backend state; authoritative project/runtime state remains backend-owned.

When debugging behavior, first identify which side owns the state transition:

- If the issue is display text, interaction wiring, or DOM updates, start in `g3ku/web/frontend/*`.
- If the issue is data shape, status lifecycle, or permissions, start in API/runtime services.

## External Agent API Assembly

The channel-agnostic headless surface for third-party bridges is mounted alongside the web/admin routers:

- `g3ku/web/main.py` mounts `g3ku/runtime/api/external_v1.py` at `/api/v1`; the global bootstrap-lock middleware (423) applies to it like every `/api/*` route. Bearer auth and bridge isolation are owned by `require_external_api` (`g3ku/runtime/api/external_auth.py`).
- Turn execution reuses `SessionRuntimeBridge` (same semantic base as `/ws/ceo`); per-session SSE streams come from `g3ku/runtime/external_events.py` hubs.
- The shared outbound drain started by `ensure_web_runtime_services` routes `channel == "ext"` messages to external session event hubs (heartbeat/cron/task-terminal proactive push), registering each one in the durable external outbox before the hub publish (startup replay re-injects unacked entries). Any other channel has no consumer and is skipped with a warning.
- The web CEO catalog treats `ext:` sessions like legacy `china:` sessions (grouped, read-only) via `is_channel_session_key`; the ext rows are registry-driven, so an external session appears in the catalog as soon as its bridge registers it, before any transcript file exists. Pre-existing `china:*` transcripts remain visible as read-only archives after the China channel subsystem removal.
- The web UI「外部接入」page (nav `data-view="external"`, `g3ku/web/frontend/org_graph_external.js`) manages `externalApi.enabled` and bridge tokens through `admin_rest` `/api/external-api/*` endpoints; token plaintext is revealed exactly once at issue/regenerate time.

Full contract (endpoints, turn terminal invariant, event mapping, outbound routing): 详见 `external-agent-api.md`.

## Local Startup And Launcher Contract

- `g3ku.cmd` / `g3ku.ps1` / `g3ku.sh` are thin CLI passthrough wrappers around `g3ku_bootstrap.py`; with no arguments they default to `web`. `start-g3ku.*` remains the explicit double-click entry with its own managed-process handling.
- For the `web` command the bootstrap parent stays in the terminal as a readiness supervisor: it spawns the server child, polls `http://127.0.0.1:<port>/api/bootstrap/status` (a g3ku-specific endpoint so a foreign process squatting on the port cannot fake readiness), and prints a terminal banner: success with the clickable URL, or failure with the child exit code plus a pointer to `.g3ku/logs/console.log` (where the child's stdout/stderr are appended). A slow-start warning appears after the probe deadline instead of a false failure.
- `g3ku/web/launcher.py::prepare_web_server_start` self-heals before acquiring the single-instance lock: it terminates leftover web-server processes of this workspace (cmdline `-m g3ku web` or `-c "...run_web_server_entrypoint..."`, scoped by the workspace venv python path or the process working directory, excluding itself). Starting therefore replaces a stale/zombie server of the same workspace; two web servers for one workspace still cannot coexist.
- The single-instance guard is `.g3ku/start.lock` (byte-range lock; metadata pid/port are written right after locking, so a holder killed between lock and write leaves an empty file and error messages may show `pid=unknown` — fall back to `netstat` on the web port when identifying squatters).
- The web port for banners and probes is read from `.g3ku/config.json` (`web.port`, default 18790). Non-`web` commands keep the plain passthrough behavior, and direct `python -m g3ku` still prints CLI help.

## Memory Management Page And Admin Contract

The browser shell has a top-level `记忆管理` page. This is intentionally a read-only operator surface for the queued long-term memory runtime.

### Frontend Responsibilities

- The left rail exposes `记忆管理` as its own top-level navigation item, not a nested subsection of model configuration.
- The page shows two independent columns: unprocessed queue items oldest-first in real queue order, and terminal processed batches newest-first, including both applied and discarded outcomes.
- Cards default to collapsed and stay compact; clicking a queue or processed card opens a frontend-owned read-only detail modal for the full payload, with long payloads kept inside scrollable text regions.
- In a processed batch detail modal, the eight meta fields (批次, 状态, 操作, 处理时间, 模型链, 请求数, 输入, 输出) render as one merged 基础信息 group, and the 变更内容 section sits above the 原始请求内容 section. The 变更内容 section renders the structured `changes` payload as one block per affected memory entry, each with its full untruncated content; rewrite entries render the original text and the modified text in a side-by-side comparison, and delete entries render the removed body. Rows whose blocks carry `original_missing` render an explicit `历史批次未保留原文`-style placeholder, and a row flagged `changes_reconstructed` shows a hint that its blocks were rebuilt from the lossy legacy preview.
- The page header's 查看记忆 button opens a read-only memory browser drawer listing the current SQLite memory rows (创建时间, 刷新值, 通过次数, 来源, ID, 记忆内容). Sort toggles for 创建时间/刷新值/通过次数 and keyword search run browser-side over the fetched snapshot; the drawer holds no mutation controls.
- `ref:note_xxxx` text is a read-only preview trigger: clicking a note ref opens a second frontend-owned drawer that only fetches and displays the note body and must not expose edit or save controls.
- The page is read-only. There are no browser buttons for retry, delete, edit, or force-flush.

### Backend Responsibilities

- `GET /api/memory/queue` and `GET /api/memory/processed` return queue-owned runtime state and terminal batch records (applied rows plus durable discarded rows), each with pagination metadata. Terminal processed rows carry a structured `changes` list — one entry per add/rewrite/delete/note_upsert with `memory_id`, full `content`, and for rewrite/delete also `original_content`. The original bodies are fetched from the SQLite store inside the commit path before the mutation applies, so each terminal row records the before/after state of every affected memory.
- Legacy processed rows that predate the structured `changes` payload are reconstructed at read time: the read path parses their flattened `change_preview` (`新增：/修改 ID：/删除 ID/更新 note ref:` segments) back into per-memory blocks and marks the row `changes_reconstructed`. Original bodies of legacy rewrites/deletes were never captured and are not reconstructible, so those entries carry `original_missing` and the frontend shows a placeholder instead of a before-text. This enrichment is display-only; the persisted `ops.jsonl` row is never rewritten.
- `GET /api/memory/current` returns a read-only snapshot of the current SQLite memory rows (memory id, body, source, refresh count, passed count, compression flag, timestamps) and backs the memory browser drawer.
- `memory/ops.jsonl` is a rolling processed-history surface rather than an append-forever archive; the backend prunes processed rows older than 7 days, so `/api/memory/processed` is the latest 7-day operator history window.
- `GET /api/memory/notes/{ref}` is the minimal read-only note preview contract, returning the note body for an existing `ref:note_xxxx` entry or a clear not-found error when the note file is missing.
- `POST /api/memory/admin/retry-head` exists as a guarded operator contract but is disabled by default; unless `G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS` is enabled the backend returns `403` with `detail.code=memory_admin_mutation_disabled`, and successful calls append an audit record to `memory/admin_audit.jsonl`.

### Maintenance Boundary

- If the memory page looks wrong but the raw JSON from `/api/memory/queue` or `/api/memory/processed` is correct, debug `g3ku/web/frontend/*`.
- If a `ref:note_xxxx` chip renders but the preview drawer cannot load, compare the frontend `ApiClient.getMemoryNote(...)` request with `GET /api/memory/notes/{ref}` before debugging the memory runtime itself.
- If the page is missing fields, ordering, discarded statuses, structured `changes`, `change_preview`, or request-artifact links already in the API response, debug the admin endpoints or `g3ku/agent/memory_agent_runtime.py`.
- If a rewrite comparison lacks the original text, first check whether the row is flagged `changes_reconstructed`: for legacy rows the placeholder is expected because their originals were never recorded. For a row committed with structured `changes`, inspect the original-body capture in the commit path of `g3ku/agent/memory_agent_runtime.py`; originals are read before the mutation applies and are not reconstructible after the commit.
- If the memory browser drawer fails to load, compare `ApiClient.getCurrentMemories()` with `GET /api/memory/current`. A `404` on that route means the running web process predates the endpoint and needs a restart; frontend files are served from disk with no-cache headers, so frontend-only edits need only a browser refresh.
- If the queue page is stuck on one `processing` batch, treat that as a backend/runtime issue first, not as a frontend pagination bug.
- Browser-side memory management remains read-only by default. If an operator expects a retry button in the UI, first check whether the feature was intentionally kept backend-only for the current build rather than debugging missing DOM wiring.

## Model Config Page And Admin Contract

The top-level `模型配置` page manages `llm-config` provider records and model bindings. Config source-of-truth, binding resolution, and secret handling are owned by `config-and-models.md`「llm_config 子系统」; this section covers the admin surface and the add/edit model workflow.

### Frontend Responsibilities

- The add/edit model modal keeps one provider-config JSON draft as its source of truth. Dedicated `请求地址` (`base_url`) and `Apikey` inputs stay two-way synced with that JSON draft. There is no manual binding-key (`模型ID`) field in create mode: the binding key is derived by the backend from the draft's `default_model`, so picking the model is what establishes its identity. Create mode opens with a required `配置名称` input (placeholder `请输入配置名称`) in place of the modal title; the name must not duplicate an existing config name (case-insensitive) and is saved into the binding's `name` field (see `config-and-models.md`「模型系统不是只靠 config.json」for the display-title priority).
- The protocol select (`协议`) defaults to `OpenAI Chat`; switching protocols only rewrites the draft's `provider_id` and preserves entered request address, Apikey, and parameters.
- The JSON region renders collapsed by default and auto-expands when draft validation or the connection probe fails.
- `获取模型列表` renders the provider catalog returned by the backend as a filterable list. Selecting an entry writes it into the draft's `default_model`, which is the fallback display title when no `name` is set.
- The model list left rail renders each binding as a compact card showing the display title (`name` > `default_model`) and the request address (`base_url`); the per-model `Chat`, `Enabled`, and role-chain-membership chips are not rendered. Editing a binding and changing `default_model` updates the stored record, so the fallback title follows the model; the binding key stays a stable identity and is not renamed.
- The detail header shows the binding's display title followed by a pencil (`编辑配置名称`) icon. Clicking it swaps the title for an inline input; Enter/blur saves the new `name` through `PUT /api/llm/bindings/{key}` (name-only payload), Escape cancels, and a duplicate name is rejected with a toast.
- The header info button next to `关闭` is a click-to-show popover (`配置说明`) layered above the modal; it does not rely on hover. Its content is the current single-key/retry/fallback contract: one API key per config, multiple configs form the model chain for fallback, retry rounds and eviction semantics, and name-uniqueness/display sync notes. The popover closes on outside click or Escape.
- Create/save of connection details rejects multiple API keys (comma/newline separated) with a single-key policy message: capacity and failover come from adding more configs to the model chain, not from multiple keys on one config. Non-retryable-key errors and 400/422 shape errors still follow the per-config chain rules (see `config-and-models.md`「Model Retry And Key Rotation Config」).
- `测试最大并发数` is folded behind a `⋯` button next to the per-key concurrency input and requires an explicit confirmation dialog before running because the escalating probe can trigger provider rate limits.
- Per-role `最大轮数` / `最大并发数` limits live in a collapsible strip on the `模型配置` page header instead of inside the role chain cards, and the strip renders only while `编辑模型链` edit mode is active. Each limit group expands to one numeric input per role; `-1` means unlimited and is persisted as `null`, while the memory Agent `最大并发数` is fixed at 1 and renders as a non-editable pill.
- The model editor carries a `思考与输出` field group in both create and detail modes: a `深度思考（Reasoning Effort）` six-level select (`none` 关闭深度思考 / `low` / `medium` 默认 / `high` / `xhigh` / `max`) and a `最大输出TOKEN` numeric input (default `65536`, minimum 1). Both fields two-way sync with the JSON draft's `parameters.reasoning_effort` and `parameters.max_tokens`, exactly like `最大上下文TOKEN`; the select stores `none` literally to disable deep thinking, and saving goes through the existing record draft save path (`POST /api/llm/bindings` on create, `PUT /api/llm/configs/{id}`-style update on edit), so no dedicated endpoint is involved. Runtime defaults and provider-side behavior: 详见 `config-and-models.md`「Model Request Parameter Defaults」.

### Backend Responsibilities

- `POST /api/llm/drafts/validate` and `POST /api/llm/drafts/probe` check an unsaved provider draft; `POST /api/llm/drafts/probe-max-concurrency` derives per-key concurrency limits; `POST /api/llm/drafts/models` fetches the provider model catalog (`GET {base_url}/models`) using the draft's credentials and rotates across multiple API keys on authentication failure.
- `POST /api/llm/bindings` creates a binding and derives a unique `key` from the record's `default_model`, appending a numeric suffix when a same-name model already exists, so different providers can share a model name. The binding payload accepts the display `name`; non-empty names are deduplicated case-insensitively across the catalog (excluding the binding being edited), and the created/updated item exposes `name` on both `/api/models` and `/api/llm/bindings`. `POST /api/llm/bindings/{model_key}/rename` remains a back-compat endpoint that renames a binding key, rewrites matching references in `models.roles.*` and `agents.multi_agent.orchestrator_model_key`, and rejects an empty or duplicate key.
- Display surfaces follow the binding `name`: the task `Token统计` breakdown's per-config heading resolves `model_key` through the catalog to `name` (falling back to the key), redrawing from `S.modelCatalog` state so renames propagate on the next catalog refresh.
- Draft endpoints validate the draft first and report field-level errors without issuing provider requests when validation fails. Draft validation normalizes endpoint-style `base_url` values (trailing `/chat/completions`, `/responses`, `/models`) to the provider API root instead of rejecting them.

### Maintenance Boundary

- If `获取模型列表` fails but `测试连接` succeeds, compare the draft `base_url`/`api_key` sync state in `g3ku/web/frontend/org_graph_llm.js` with `POST /api/llm/drafts/models` before debugging the provider.
- If catalog fetch returns a non-JSON or empty-catalog error, treat it as a provider endpoint-shape problem (same triage as a failed model-catalog connection probe), not as a frontend bug.

## Frontend I18n Runtime And Language Switching

The frontend language switcher is architecture-relevant because it changes operator-visible workflow and UI state behavior.

### Core Runtime Pieces

- `g3ku/web/frontend/locales/zh-CN.js` and `g3ku/web/frontend/locales/en-US.js` register locale dictionaries into `window.G3KU_LOCALES`.
- `g3ku/web/frontend/i18n.js` exposes `window.G3KUI18n` and applies translations to:
  - `data-i18n` text content
  - `data-i18n-placeholder` placeholder text
  - `data-i18n-aria-label` accessibility labels
- Locale preference is persisted in browser storage under key `g3ku.ui.locale.v1`.

### Shell Integration Flow

1. `org_graph.html` loads locale files before `i18n.js` so dictionaries are available during i18n initialization.
2. `i18n.js` resolves locale from persisted value or fallback locale and applies translations.
3. The shell language `<select id="language-switch">` calls `window.G3KUI18n.setLocale(nextLocale)` on change.
4. `i18n.js` emits `g3ku:locale-changed` after successful locale changes.
5. Shell listeners update locale-linked UI state (for example, `<html lang=...>` and switcher selection sync).

### Maintenance Caveats

- Script order is contract-sensitive: locale dictionaries must load before `i18n.js`.
- New frontend copy should use translation keys rather than hardcoded language strings.
- If new controls need localized placeholders or ARIA labels, use the existing `data-i18n-*` attributes.
- Locale persistence is browser-local; no backend API currently stores per-user UI locale.

## Operator-Visible Behavior

- Operators can switch between Simplified Chinese (`zh-CN`) and English (`en-US`) from the shell footer.
- The selected locale persists across page reloads for the same browser profile.
- Runtime-generated labels/messages that depend on `window.G3KUI18n.t(...)` update to the active locale without requiring backend restart.

## CEO Composer Runtime

The Leader/CEO composer has two distinct runtime behaviors that maintainers need to keep straight.

### 1. Active-Turn Button Semantics

- If the current session is idle and the composer is empty, the primary button stays in a disabled `send` state.
- If a user-visible turn is currently running and the composer is empty, the primary button switches to `pause`.
- If the composer contains text or attachments, the primary button switches back to `send` even when a user turn or heartbeat turn is still running.
- Read-only channel sessions (`china:*` / `ext:*`) keep the button disabled with the read-only label while idle, but while a channel session has a running turn the same button switches to `pause`: the readonly constraint blocks sending (`client.user_message` is rejected with `channel_session_readonly`) and must never block pausing. The pause request is the same `client.pause_turn` lane as local sessions.

This is intentional. The composer button means "pause only when the user has not prepared a follow-up payload", not "pause whenever a turn is active".

### 2. Queued Follow-Ups

- Browser-side queued follow-ups are stored per session and rendered above the composer.
- Sending while a turn is active still does not interrupt the current turn, but the browser forwards that follow-up to the backend immediately instead of waiting for the turn to go idle first.
- The backend-owned follow-up queue is authoritative once that send succeeds. The local chip list remains a UI affordance only, and chips that were already handed off to the runtime must not be re-sent when the turn later closes.
- CEO/frontdoor consumes queued follow-ups at the safe boundary right before the next `call_model` send of the same visible turn. The runtime appends them as independent `user` messages to the current request body instead of concatenating them into one synthetic supplement string.
- If the current visible turn finishes before another `call_model` round happens, the backend immediately starts the next fresh user turn from the queued follow-ups after the current turn closes.
- Each queued item still remains its own user message in transcript persistence; batching only changes which next LLM request sees that group first.
- A single visible turn can hold multiple distinct user messages once follow-ups are consumed. The browser treats two user messages as equivalent only when their turn id (when present on both), content, and attachments all match; matching on turn id alone drops a distinct follow-up and leaves the user's supplement invisible.
- Once a queued follow-up has been handed off to the backend, it should still remain in the composer-side queue lane until runtime snapshot/final-reply data can prove that the follow-up has been consumed into a visible turn.
- Browser rendering must not create a provisional transcript bubble merely because a follow-up was accepted by the backend queue. Queue acceptance and visible conversation placement are intentionally different stages.
- `ceo.reply.final` may include `user_messages` for the just-finished visible turn. This is the authoritative current-turn user batch and exists specifically so the frontend can decide whether runtime-sent follow-ups belonged to that same reply or to a later chained turn.
- If `ceo.reply.final.user_messages` includes a runtime-sent follow-up, the browser must rebuild the final visible order as `current turn user batch -> final assistant reply` and clear the matched queue entry.
- If a runtime-sent follow-up does not appear in the just-finished turn's `user_messages`, the browser should keep it in the queue lane until a later fresh user turn or transcript snapshot represents it authoritatively.
- `snapshot.ceo.messages` must also avoid replaying running-turn `pending` user transcript rows as ordinary history bubbles. During a live running turn, authoritative current-turn user placement comes from `inflight_turn.user_messages`, not from flat transcript replay.
- When a running follow-up is actually consumed into the next model send of the same visible conversation lane, the runtime also archives the pre-follow-up assistant execution bubble into visible UI history before the consumed user bubble is restored. That archive is UI-visible but prompt-hidden, so refresh/reconnect can preserve the same visual ordering without polluting later prompt history.
- The archive turn id carries a `:followup:` segment. When the browser renders an archived turn with that segment, it marks the last stage in that archive as the interrupted stage (interruption marker in the stage title), because a follow-up consumed mid-flight splits that stage between the archive (rounds already done) and the continuation turn (remaining rounds).
- A consumed follow-up's user bubble is inserted immediately before the current live turn element, not appended to the end of the feed, so it reads in chronological order between the archive and the continuation.

### Model Retry Visibility UI Contract

- `ConfigChatBackend` publishes live-only `model_retry_status` before each real retry (per-model backoff round or same-model key-switch resend) and clears the field when retrying exits. A cross-model fallback is normal chain operation: it is counted but emits no status. The status carries `state=retrying`, `retry_count` equal to provider requests actually sent so far (hence the ordinal of the retry about to happen), the model retry round, the model refs, an `error_message` capped at 4096 chars to keep websocket payloads bounded, the backoff delay (0 for key-switch emissions), and local-offset `last_retry_at` / `next_retry_at` timestamps. The task-node frame sanitizer (`_sanitize_model_retry_status`) is a field whitelist — the timestamps must be listed there or they are stripped — and its error-text bound equals the emit-side limit because the browser expands the toast to the full received text. The CEO snapshot deep-copies the whole status and needs no whitelist entry.
- For CEO sessions, running `inflight_turn.model_retry_status` reaches the browser through the `snapshot.ceo` / `ceo.turn.patch` lanes. The browser shows a warning toast above the conversation feed while `state=retrying`, shaped `模型自动重试中 · 第 N 次重试 · 最新 HH:MM:SS · 下次 HH:MM:SS · <error>` (clocks render only when present); the toast disappears when a snapshot clears the field or the session leaves the running state. Long error text is visually clamped to two lines with an expand affordance (click or Enter/Space toggles the full text).
- For task nodes, the runtime frame exposes the same field through `task.live.patch`. Node detail reads the selected frame and shows the same toast below its header. Live patches update the toast while the detail drawer is open, and opening the drawer during a retry restores it from the current frame.
- The status is transient observability state, not transcript history. It is not appended to persisted CEO messages, does not backfill historical node output, and is cleared on success, terminal failure, pause, discard, or config-refresh-based turn rebuild. A failed websocket delivery is covered by the next low-frequency snapshot/patch or by reopening the session/node while the runtime is still retrying.
- If the chat is stuck but no retry toast appears, check that the actual failing boundary is the retryable `ConfigChatBackend` path rather than an outer idle/preflight/tool wait, then inspect the `on_model_retry_status` callback, `inflight_turn.model_retry_status`, and the node runtime frame field in that order.

### 2.2. Regulatory Approval Flow

- Web CEO has a second blocking composer lane besides ordinary running turns: a pending `frontdoor_tool_approval_batch`.
- The websocket/runtime path emits that batch through `ceo.turn.interrupt`, and reconnect/session-restore should also be able to rebuild it from `GET /api/ceo/sessions/{session_id}/pending-interrupts` or the paused snapshot lane.
- The frontend review UX is intentionally split:
  - the operator reviews one risky tool call at a time in a toast-like approval card,
  - but the browser must not resume the agent per item.
- The authoritative resume payload is one batch submission:
  - `client.resume_interrupt`
  - `resume.type="submit_batch_review"`
  - `resume.batch_id`
  - `resume.decisions=[{tool_call_id, decision, note?}, ...]`
- The browser may let the operator move backward, change earlier choices, and keep a session-local draft, but it must submit a complete decision for every `review_item` in the batch before the backend resumes.
- Rejection notes are optional operator text, but they only belong to rejected items. The frontend should not send a `note` field for approved items.
- Clicking the parameter preview opens a separate full-args modal. Long argument bodies must stay scrollable, and clicking outside that modal should close it.
- While a regulatory approval batch is pending, the composer should behave as blocked-active state rather than as ordinary paused state:
  - no new user message send,
  - no queued follow-up dispatch,
- session/page switching is allowed while the batch is pending, but the pending review must remain session-local draft state and reappear when the operator returns to that CEO session.
- Frontend pause rendering must not rely only on `source="approval"`. Approval pauses may arrive with ordinary `source="user"` plus approval interrupts in the inflight/paused snapshot lane; in that case the browser should keep the current visible turn in the approval-waiting state instead of finalizing a new `已暂停` history bubble.
- Once the operator submits the batch, the browser should clear the local draft for that batch and wait for the normal runtime events (`ceo.state`, `ceo.reply.final`, etc.) to continue the conversation.

### 2.3. Streamed Reply Delta Contract

- `/ws/ceo` remains the single duplex transport for Web CEO. Browser sends, pause/resume control, tool events, and assistant reply delivery still share the same websocket connection; streamed assistant text is not a separate SSE channel.
- User-visible assistant text may arrive on a dedicated live-only event `ceo.reply.delta`.
- The payload is intentionally small:
  - `turn_id`
  - `source`
  - `text`
  - `seq`
- `text` is the authoritative assistant text of the stream's latest thinking segment: the previous segment stays visible until the next model-call boundary resets it, so the live block shows only the latest segment rather than a whole-turn accumulation; it is neither a raw provider token nor a stage-trace snapshot.
- The browser should treat `ceo.reply.delta` as a cheap text-only lane:
  - render the latest-segment text as a transient live block inside the turn's stage rail (`.task-trace-live-text`), not into the final assistant bubble; the bubble only receives the authoritative final text at `ceo.reply.final`
  - update cached `inflight_turn.assistant_text` for reconnect/session restore
  - never treat the delta as durable transcript history and never rerender the stage trace from it
- Streamed text renders through a plain-text path while the turn runs; `ceo.reply.final` remains the authoritative closeout, where full markdown render, transcript finalization, canonical-context finalization, and visible-turn completion happen.
- The CEO turn body renders as the turn's stage timeline, not as a single overwritten bubble; see "CEO Turn Timeline Rendering Contract" for the layout and field rules.
- The projected `canonical_context` on assistant transcript entries carries the retained raw stage window and per-round narration across reloads; older completed stages remain compact summaries.
- `ceo.turn.patch` is not the high-frequency assistant-text streaming lane; it is the lower-frequency lane for inflight/preserved snapshot refreshes, state transitions, reconnect bootstrap, tool/interrupt-related snapshot changes, and stage-state progression: the frontdoor graph emits `frontdoor_stage_synced` after each graph-node boundary sync of the session-stage/canonical state, and the websocket relay answers with an immediate live turn patch, so a newly submitted stage and its first tool rounds reach the browser without waiting for the next tool event.

### 2.4. Runtime Error Contract

- `/ws/ceo` may also emit `ceo.error` when a visible turn fails before `ceo.reply.final`.
- The frontend should treat `ceo.error.data.message` as the authoritative operator-facing text for that failure boundary, not as optional debug metadata.
- Backend error delivery has an explicit empty-message fallback. If the raw exception string is empty (for example a bare `MemoryError`), websocket delivery must reuse the session snapshot `last_error.message` when available, and otherwise emit a non-empty fallback message instead of leaving the browser to show `unknown error`.
- This matters especially for memory-pressure failures during request-artifact persistence: the operator-visible contract is “a readable runtime failure message plus a terminal error state”, not “blank message that the browser turns into `unknown error`”.

### 2.5. Image Upload Gating

- Whether uploads become provider-visible image content is gated per turn by the selected model binding's `image_multimodal_enabled` flag: off keeps uploads on the text downgrade path; on lets only the current visible turn's live request carry provider-visible image input, while durable transcript/baseline lanes strip the image blocks back out.
- A stable reopen lane, `attachment_reopen_targets`, rebuilt from current-turn uploads and earlier transcript metadata, lets uploaded files/images be reopened after the user text is rewritten into a direct-visual note. Detached task creation does not auto-fill from this lane; `create_async_task.file_targets.path` must already be the exact absolute path of an existing file.
- Upload protection is two-layered: `/api/ceo/uploads` rejects a single image larger than `5 MiB`, and the runtime rechecks size before expanding an upload into a provider request, so bypassing the endpoint does not bypass the limit.
- Historical reopen via `content_open` is a separate lane that injects a single-send runtime overlay plus the provider-visible image block; the overlay does not survive a later overflow, compression, or error, and a non-multimodal binding fails rather than degrading to a text preview.

### 2.6. Attachment Bubble Rendering Contract

- Browser-side CEO message rendering must treat uploaded attachments as structured UI, not as plain transcript text.
- When a user message contains both text and attachments, the frontend should render:
  - the user text as the normal user text bubble
  - the attachments as separate attachment bubbles stacked directly below that text bubble in the same message lane
- When a user message contains attachments but no user-visible text, the frontend must render only the attachment bubbles. It must not synthesize a summary bubble such as `已附加附件`, and it must not surface the backend/internal `Uploaded attachments: ... local path ...` note as chat content after refresh or reconnect.
- The backend snapshot contract behind that rule is:
  - transcript/runtime messages may still carry the internal upload note in `content` for model/debugging purposes
  - `snapshot.ceo.messages[].content` must prefer `metadata.web_ceo_raw_text` whenever `metadata.web_ceo_uploads` is present, even when that raw text is the empty string
  - the authoritative user-facing attachment lane comes from `attachments`, not from reparsing the internal note text
  - that same persisted metadata lane is the source of truth for rebuilding `attachment_reopen_targets` in later frontdoor turns; if reopen paths break while attachment bubbles still restore, debug transcript metadata preservation first
- Non-image attachments should render as clickable file bubbles rather than inline text. Clicking them should open a new browser tab against the backend-owned read-only file route `GET /api/ceo/uploads/file`, not against a raw local filesystem path.
- Image attachments should render as thumbnail bubbles, not as ordinary file pills. The same thumbnail should also be the click target that opens the underlying file in a new tab.
- If refresh/reconnect shows the internal upload note instead of attachment bubbles, debug the snapshot builder in `g3ku/runtime/api/websocket_ceo.py` before debugging CSS or DOM layout.

### 2.7. Inline Markdown Image Rendering And Media Middle Layer

- The chat markdown renderer (`renderInlineMarkdown` in `g3ku/web/frontend/org_graph_app.js`) renders `![desc](src)` as an inline image for both persisted history and live turns: `https`/`http` URLs render directly, local filesystem paths resolve to the read-only route `GET /api/ceo/uploads/file`, and `data:` URIs or unknown schemes render nothing. This inline path complements, rather than replaces, the structured attachment bubbles in section 2.6.
- That file route serves from two allowed roots only — the per-session upload directory and `workspace_path()/output` — returning `400 upload_path_outside_session_dir` for paths outside either root and `404` for missing files.
- Most assistant local references are rewritten at the snapshot egress by the media middle layer (`g3ku/runtime/api/ceo_media.py`) so they become servable without touching the on-disk transcript: raster images become staged thumbnails inside an already-allowed root, other local files become signed viewer URLs, and URLs, already-servable references, and failed rewrites pass through unchanged.
- Originals are reachable only through the signed viewer route `GET /api/ceo/media/original?token=...`, which takes an unguessable signed token rather than a caller-chosen path. Because only raster image bytes are ever staged into a serving root, the endpoint cannot become an arbitrary file oracle.

### 3. Context Loader Notices

- Successful CEO/frontdoor `load_tool_context` and `load_skill_context` calls do not render as ordinary `Interaction Flow` steps under the assistant bubble.
- Frontend loader-notice detection must treat both legacy and v2 loader names as the same UI family:
  - `load_tool_context` / `load_tool_context_v2` => tool notice
  - `load_skill_context` / `load_skill_context_v2` => skill notice
- Instead, the browser shows a short-lived composer notice above the input row, using the loaded `tool_id` or `skill_id` when the runtime payload exposes it.
- These notices are intentionally stackable rather than single-slot: multiple successful loader calls may coexist in one right-aligned floating column that lines up with the send-button edge.
- The type-specific styling comes from a leading icon rather than a leading green dot:
  - tool notices use the same `wrench` icon family as the sidebar Tool page
  - skill notices use the same `sparkles` icon family as the sidebar Skill page
- The risk-colored dot remains present on the trailing edge so operators can still distinguish low / medium / high loader risk at a glance.
- The intended motion contract is still "launch from the composer, settle into the notice stack, then fade out"; the full lifecycle is currently about 5 seconds per notice.
- That notice is intentionally live-only UI state. It should fade away after a short timeout and must not be appended into the persisted CEO session `messages` list.

### Manual Pause Resume Rule

- Manual pause means “freeze the current round as previous-round context,” the operator-visible “pause” button is a terminal stop for the current visible turn, the runtime state ends as `completed` tagged `stop_reason=user_pause`, and ordinary user sessions are never left in a long-lived resumable `paused` state. The next outbound user message after pause must start a new round.
- The paused round's user message, execution trace, stage state, tool calls, and compression state are preserved in transcript and snapshot context so the next round can inherit them without rewriting the original user text.
- Manual pause writes the completed-session continuity sidecar immediately, clears that session's ordinary paused/inflight restorable snapshots, and archives the paused assistant bubble into a persisted message with `status=paused` during the stop flow itself, not only when the next user turn is about to dispatch.
- That archived paused assistant is durable UI history for `snapshot.ceo` restore/reconnect, but it remains hidden from prompt-history assembly and session-summary counts via `history_visible=false`.
- Browser-side restore should therefore render that persisted paused assistant as a paused bubble rather than a completed reply. The next ordinary user turn inherits context from visible history plus completed continuity state, not from resuming the old paused turn.

### CEO Stage Trace Round Rendering Contract

- The browser CEO stage view should treat `canonical_context.stages[].rounds[].tools` as the authoritative round-level tool list.
- Refreshing the page or reopening a completed session should reproduce the same round grouping that live inflight snapshots used; the frontend should not try to regroup same-name tools on its own.
- `tool_names` and `tool_call_ids` may still be present for compatibility, but they are summary metadata rather than a second grouping algorithm.
- The stage progress badge in both the CEO session view and the shared task-trace components must reflect budget-counted rounds rather than raw round history length.
- The shared stage body renderer (CEO turn timeline and task node detail flow alike) displays, inside each expanded stage in order: the stage preamble (CEO stages), each round's `rounds[].text` (the model's mid-round narration) above that round's tool chips, and the stage's `completed_stage_summary` as a trailing 阶段总结 block; a round without text renders tool chips only. The task node detail payload carries these fields at both detail levels: `execution_trace` (full) and `execution_trace_summary` (summary) both keep `stages[].completed_stage_summary` and `stages[].rounds[].text`.
- Frontend progress rendering should use `tool_rounds_used` as the primary source, and only infer a fallback count from `rounds[].budget_counted=true` when an older payload lacks an explicit count.
- Do not derive displayed progress from plain `rounds.length`: successful `load_tool_context` / `load_skill_context` rounds may remain in history for auditability while being hidden from visible execution chips, and treating raw round count as budget usage will overstate progress.

### CEO Turn Timeline Rendering Contract

- The CEO session view renders each visible turn as a timeline: the user message right-aligned, then the turn's own stage rail (stage titles collapsed by default with budget/progress meta, expandable to rounds; a round with no mid-turn text renders tool cards only), then the final assistant output; the legacy wrapper element remains only as the rail container.
- Live inflight turns resolve stage data from the per-turn `canonical_context_delta` first, then the turn's own rendered trace summary, and never from the session-cumulative `canonical_context`: `_frontdoor_stage_state` accumulates across turns, so a full-context fallback leaks the previous turn's stages into a new live turn. A patch whose `turn_id` differs from the reused turn clears that turn's rendered trace summary and live stream text before resolving context.
- Each live patch merges its delta into the turn's already-rendered trace summary (stages and rounds merge by identity, stage headers take the delta) instead of replacing the timeline, so completed stages stay visible while later phases render. A patch carrying no delta is a no-op for the trace: the browser keeps rendered stages and live tool steps instead of resetting to the "waiting" placeholder, preserving mid-turn stage submissions between graph-node stage refreshes.
- Deltas are produced server-side by `ui_canonical_context_delta(...)` through the canonical projection contract owned by `runtime-overview.md` "CEO Frontdoor Canonical Context Contract": projection keeps historical stages stable, so a new turn does not re-expand compact stages into the newest bubble.
- Persisted assistant messages keep projected cumulative `canonical_context` (`canonical_context_projection: stage_window`) and message-local `canonical_context_delta`; refresh and reconnect render the delta, and inflight/paused/final payloads use the same UI projection (`project_canonical_context_for_ui_payload(...)`), with raw-window round bodies reattached from the live snapshot.
- `rounds[].text` carries the model's mid-turn narration for that tool round; a stage's `preamble_text` carries the narration from the response that created the stage (rendered above the stage title). Both are display-only fields of the stage state; prompt assembly, transcript durability, and archive semantics do not depend on them.
- `ceo.reply.final` remains the authoritative closeout (final markdown plus final delta/trace when present); the payload shape is defined by the backend contract below.
- The frontend must not reconstruct CEO stage flow from flat `tool_events`; `canonical_context_delta` is computed server-side from message order, so refresh/reconnect rebuild the same per-bubble trace slices without a browser-local cursor.
- Tool output rendering follows canonical payload fields: inline `output_text` renders directly; an externalized or transcript-capped body renders preview and the artifact-open path. The frontend must not invent extra truncation or backfill old tool-event text.

The backend contract behind that UI behavior is:

- CEO/frontdoor runtime writes precise round tool entries into `frontdoor_stage_state.stages[].rounds[].tools` when a tool cycle finishes.
- Session snapshot assembly trusts stored `round.tools` first and only backfills legacy rounds by exact `tool_call_id`.
- A `tool_name`-only fallback is considered a regression because it can make a later same-name tool appear inside an earlier stage round after refresh or transcript reload.
- The browser still treats `round.tools` as authoritative input, but it filters successful `load_tool_context` / `load_skill_context` entries out of the visible stage-trace tool chips because those calls represent context acquisition rather than user-facing execution work.
- `ceo.reply.final` canonical-context shape: the shared assembler `final_reply_canonical_merge` (`g3ku/runtime/web_ceo_sessions.py`), used by the userspace relay and the heartbeat publisher alike, includes the UI-projected `canonical_context` only when the completed turn added stage progress over the previous assistant message, omitting both `canonical_context` and `canonical_context_delta` otherwise (the delta uses the same `ui_canonical_context_delta(...)` view, so a closeout never re-emits historical stages). Browsers prefer that payload over any older inflight or persisted trace; a message whose delta renders empty stages shows a plain text bubble with no stage rail, and neither `finalizeCeoTurn` nor `renderPersistedCeoAssistantTurn` may fall back to `turn.lastExecutionTraceSummary` or the session-cumulative `canonical_context` — backfilling an older trace under a new direct-reply bubble is a contract bug.

### CEO Feed View State And Scroll Preservation Contract

- `renderCeoSnapshot` is a full feed rebuild (message replay plus inflight/preserved turn restore) used for session loads, session switches, and as the authoritative fallback whenever the rendered DOM can no longer be trusted. Rebuilds are avoided when unnecessary: an incoming `snapshot.ceo` whose render signature (full messages projection plus live turn key fields) equals the current render is skipped outright, so reconnect re-pushes do not churn the feed; and `ceo.reply.final` closeouts carrying `user_messages` update incrementally — missing user bubbles are appended before the running turn and the turn finalizes in place — whenever the rendered DOM exactly matches the recorded message-key model (each child carries its `data-ceo-key` in order, the final child is the active turn). Any mismatch (promoted follow-ups, hand-sent bubbles, preserved-turn coexistence) falls back to the full rebuild, which stays the authoritative self-healing path.
- Both full feed rebuilds and live turn re-renders must preserve the operator's reading position and expansion choices. Live updates (`ceo.turn.patch`, `ceo.reply.final`) re-render the turn's whole stage track through a wipe-and-rebuild (`renderCeoStageTraceIntoTurn`), so a turn-scoped capture/restore contract applies to every such re-render, not only to snapshot rebuilds.
- Before a live stage-track re-render wipes the turn DOM, the browser captures turn-scoped state: Interaction Flow open/closed, per-stage `<details>` open keyed by `data-trace-key`, and the round tool strip selection keyed by owning-stage trace key + `data-round-key` (in-turn host-order fallback when the round key is empty, stage-scoped because `round_index` repeats across stages). Restore runs right after the rebuild and before the `[open]` step hydration pass, so re-opened stages re-fetch output blocks and a restored tool selection re-activates its panel through `setTraceRoundActiveTool`. Capture is gated on `turn.lastExecutionTraceSummary`: a turn-id change clears that field, so a new turn never inherits the previous turn's expansion state (stage ids repeat across turns). A turn's first track render uses the defaults — flow open, stages collapsed.
- Turn closeout respects the operator's Flow container choice when a stage track exists — `finalizeCeoTurn` does not force the container open; a track-less tool-event turn keeps its default-open closeout. Pause/approval holds still force the container open so a pending approval stays visible.
- Before a full rebuild, the browser captures a transient per-session view state from the live DOM: the anchor element (`data-ceo-key`, with a bottom-relative positional fallback) plus its in-element scroll offset; per-turn Interaction Flow open state and `展开全部` history expansion; per-stage `<details>` open state (scoped per turn because `stage_index` fallback trace keys collide across turns); and the active round tool chip (scoped by per-turn DOM order because an empty round key would collide across rounds).
- After the rebuild, the browser restores that state onto the new DOM and re-syncs the scroll position to the anchor element's new geometry (element-anchored, not pixel-preserving), re-pinning once more after a double rAF so late media/lazy output blocks settle; restored-open steps re-run output hydration because assigning `open` directly skips the toggle listener, and restored round tool selection re-activates its panel through `setTraceRoundActiveTool`. Operators following the bottom keep bottom-follow behavior; a missing anchor falls back to pixel clamp.
- Every feed mutation runs under a preserve-mode scroll snapshot (`captureCeoFeedScrollSnapshot` / `restoreCeoFeedScrollSnapshot` in `mutateCeoFeed` / `withCeoFeedBatch`): an at-bottom operator (`ceoFeedNearBottom`) follows new content to the bottom (stick-to-bottom), and a scrolled-up operator gets the position restored against the anchor element's post-mutation geometry, falling back to pixel clamp when the anchor is missing. The live follow pins synchronously without the async re-pin of `scrollCeoFeedToBottom`, so a pending frame cannot yank the operator back after they scroll up mid-stream.
- Stable identity is mandatory for rebuilt feed nodes: messages get `data-ceo-key="m:{turn_id}:{role}:{occurrence}"`, turns get `data-ceo-key="turn:{turn_id}"`, and the recorded message-key model (`S.ceoFeedRenderedMessageKeys`) plus the render signature are updated by every render path (full rebuild, incremental finalize, and the skipped-snapshot fast path alike). New render paths that add feed nodes without these keys, change stage `traceKey` composition, or fail to maintain the recorded model silently weaken scroll anchoring, rebuild-stable expand state, and the incremental-finalize gate.
- Capture is per-session and is skipped whenever the feed still shows a different session than the render target (`S.ceoFeedRenderSessionId`), so session switches and new-session loads never clone the previous session's view state.

### Per-Turn Token Usage Contract

- Each assistant turn renders a small per-response token line under the final bubble (`输入/缓存命中/输出`), built from a normalized usage object `{input_tokens, output_tokens, cache_hit_tokens, call_count}` that maps directly onto normalized provider usage (`g3ku/providers/base.py`): `input_tokens` is the uncached lane, `cache_hit_tokens` the cache-read lane, `output_tokens` the generated output; no cost or cross-lane arithmetic is done in the browser.
- Visibility is state-dependent: while a turn runs the line is always visible and updates live; once the turn settles (finalized, paused, or loaded from history) it collapses and reveals instantly on hover or keyboard focus of the bubble. A turn with no observable usage keeps the line hidden.
- The usage object reaches the browser through the same CEO websocket lanes as the turn it describes: `snapshot.ceo.messages[].usage` for persisted history, `inflight_turn.usage` / `preserved_turn.usage` for live turn patches, and `ceo.reply.final.usage` for the authoritative closeout.
- Live-turn value comes from the session's in-memory per-turn accumulator (`RuntimeAgentSession._frontdoor_turn_usage`, keyed by `turn_id` and incremented once per visible frontdoor `call_model`), exposed through `_build_execution_context_snapshot`. Reopening a completed session aggregates the same three fields from the persisted actual-request artifacts by `turn_id` (`read_session_turn_token_usage`); the two sources produce the same shape, and the aggregation participates in nothing else — baseline/handoff and cache-family decisions never read it.
- The token line is display-only telemetry. It is not prompt-assembly input, not a shrink reason, and must not be used to gate rendering, queueing, or approval behavior.

### Heartbeat Compatibility

- Heartbeat turns count as active session work for the composer button and queueing logic.
- Queued follow-ups should not interrupt heartbeat execution.
- Active-turn follow-ups are forwarded to the backend during heartbeat execution as well. They may be merged into the next safe `call_model` boundary of that same running turn, or become the next fresh user batch immediately after heartbeat finishes if no same-turn model round remains.
- Terminal-event delivery is idempotent per session: a re-delivered `task_terminal` whose `dedupe_key` already produced a visible reply is acknowledged silently without running the agent or emitting a second `ceo.reply.final`, so the user sees that reply from the transcript snapshot instead of a live push, and the transcript stays authoritative. Key derivation, the durable callback/outbox boundaries, and the persisted `handled_terminal_dedupe_keys` list belong to `heartbeat-system.md`「Task Terminal Repair Contract」.

### Task Hall Action Contract

- The browser task hall exposes only `pause`, `resume`, and `delete` task actions; `retry`, `continue-evaluate`, and `open continuation` are absent from the UI and REST surface.
- Status pills derive from the current task `status` plus final-acceptance state. The multi-select menu uses the backend buckets `已暂停` / `完成` / `未读` / `失败` / `未通过` / `进行中`; `完成` maps strictly to `success`, and `未通过` maps strictly to `unpassed`.
- Batch delete sends one `POST /api/tasks/bulk-delete` request with `task_ids`. The per-task response is authoritative: inspect each `items[]` result (`deleted`, `not_found`, `failed`) before choosing toast behavior.
- The task detail tree exposes node pause/resume controls; pause is local by default, and cascading asks about all descendants including inspection nodes. The backend stores `pause_requested`, `is_paused`, `pause_reason`, and an optional remark; a non-cascaded child continues until its parent reaches a safe boundary.
- When a node has an active or waiting child, the browser uses the shared centered confirmation modal (not a native dialog), explaining that pausing the parent does not stop children and exposing an unchecked `同时暂停所有子节点（包括检验节点）` option; confirming sends the node-pause request with `cascade` equal to that checkbox, cancelling sends nothing.
- During task-level pause, the tree displays every non-terminal node as `任务暂停` via frontend projection; node pause fields remain unchanged, and recovery reveals any pre-existing node pause.
- The `错误日志` drawer reads `GET /api/tasks/{task_id}/error-log`, shows time, node, and error text, and treats the node id as a navigation target. Clicking it runs the shared tree-node locate pipeline (`locateTaskNode`): each ancestor's round selection switches to the round that contains the next hop, the ancestor's subtree is refetched with that round id (`GET /api/tasks/{task_id}/nodes/{node_id}/tree-subtree?round_id=...` — fetching the default round instead would delete old-round children from the frontend snapshot), then the node is centered and highlighted once; a node that cannot be displayed in any tree resolves to centering its nearest visible ancestor instead of failing silently. Pause/resume changes arrive through the task-node patch/snapshot path, not by reconstructing state from raw storage tables.
- 任务树左上角悬浮节点搜索框：输入节点 ID 或 goal 关键词后，在前端快照（`nodes_by_id`）中按“精确 > 前缀 > 包含”（ID 与 goal 同分优先 ID）排序展示候选，每条显示 goal、节点 ID 与状态。点击候选走同一定位管线并把缩放固定到 `TREE_FOCUS_SCALE`（1.2）以便看清节点；定位不清空搜索框内容与结果列表，方便在候选中连续跳转。定位命中的节点播放一次性放大脉冲动画（`.task-tree-node-locate`：放大到 1.18 倍并保持，配 #39c5bb 光圈后回落，约 1.2s）；动画窗口期记录在 `S.treeLocateHighlight`，整树重渲染后以负 `animation-delay` 补挂并从当前相位续播，保证运行中任务的高频重渲染不会吞掉高亮。因跳转到旧轮次而产生的手工轮次选择会让「回到最新树」按钮出现，点击它恢复所有节点的最新树视图。
- Task pause is a synchronous experience with an in-card hint: after a single-card or batch `pause` request succeeds, that task's card renders a small hint box (not a page-top toast) showing `暂停中 (stopped/total)` with a spinning icon and a `恢复运行` button while the backend reports draining, and switches to `暂停成功` (auto-hidden after ~2.5s) once draining ends; the page-top success toast is suppressed because the hint owns that feedback. Truth comes from `GET /api/tasks/{task_id}/pause-state` (`internal_total` / `internal_stopped` / `draining`), polled every 400ms with a ~30s cap: `draining` is true only while that task's `pause_task` command is still unfinished and a live worker exists, so `暂停成功` means the worker actors truly stopped rather than only the durable flags being set. The `恢复运行` button runs the ordinary resume path — worker command FIFO guarantees the resume lands after any still-pending pause command.
- 任务磁盘治理 UI（契约本体见 `runtime-overview.md`「磁盘写保护与治理」）：任务大厅性能条含「磁盘剩余」水位列（紧急=critical、清理线=throttled 着色，数据来自 `GET /api/tasks/worker-status` 的 `machine_disk_free_bytes / machine_disk_usage_percent / disk_emergency_active / disk_cleanup_active`，1s 轮询），紧急态在任务网格顶部渲染红色横幅。归档状态以卡片**左上角外侧圆形角标**呈现（`.pc-corner-badge--archived`，lucide `archive` 图标；顶栏内文字 chip 在窄卡片会被挤成竖排故弃用）；「已清理」墓碑仍是顶栏内文字 chip。卡片 `.pc-topbar-meta` 内有书签按钮（`POST /api/tasks/{id}/pin`，pinned 任务豁免自动压缩/清理）；kebab 菜单含 `固定/压缩归档/解压` 动作（`POST /api/tasks/{id}/compress|decompress`，web 模式经 worker 命令转发，120s 超时）。打开或恢复已压缩任务先解压：打开走 `requestInlineConfirm` 确认，恢复直接触发后端自动解压，两者都渲染 pause-hint 管线的「已压缩，正在解压…」态（轮询列表 `archived` 翻转，120s 上限）；解压预检失败（`insufficient_space`/`pending_decompress`）提示清理磁盘、任务保持暂停。`archived/pinned` 字段经 WS `task.summary.patch` 推送并参与 fingerprint 与 `taskCardPatchEligible`/`taskGridRenderSignature` 判定（漏注册会导致卡片不刷新）。

### Node Detail Error History

- The node detail drawer embeds a `历史错误记录` section between `验收` and `日志`. It reads `GET /api/tasks/{task_id}/nodes/{node_id}/error-log` and lists that node's raw records (time, node title, full error text) newest-sequence first; an empty node shows `暂无历史错误`, and a header `刷新` button refetches.
- History rows are the same `task_error_logs` records as the task-level `错误日志` drawer, filtered by node. They are durable history, not live-only state: they persist while the task exists and are deleted with the task.
- The runtime appends a record for every invalid final-result submission while the node is still running (not only when the node dies): the entry carries submission count, `response_tool_call_count`, tool names, `output_tokens`, `finish_reason`, provider model and `sent_max_tokens`, plus a `疑似触及输出token上限被截断` marker when `output_tokens` reached the sent cap — enough for operators to confirm truncation or no-tool-call failures without reopening request artifacts.

### Task Message Distribution UI Contract

- Task-tree distribution UI is task-wide, not root-only: the frontend treats `runtime_summary.distribution.mode == "task_wide_barrier"` as the authoritative banner source instead of inferring state from the root node's pending-notice count alone.
- While the task is in `barrier_requested`, `barrier_draining`, or `distributing`, the task-tree view shows a task-local sticky notice (not a global shell toast) and the execution-tree wrapper switches connector lines into a yellow distribution mode. The notice disappears once the target node exposes the pending message through its backend-owned message list / `pending_notice_count`; a node-level `distribution_status` of `barrier_blocked` renders as a yellow warning.
- A distribution `state == "failed"` renders the same sticky notice in a red failed variant (`task-tree-distribution-bubble--failed`) whose text carries `distribution.error_text` plus the intervention hint (re-append the notice to retry, or resume the task), without switching connector lines into distribution mode. An explicit resume downgrades the banner to the ordinary pending-notice state (`resume_ready`). Failure semantics are owned by `runtime-overview.md`「frontdoor 与任务运行时的关系」.
- Node detail receives a backend-owned message list (its own section before `派生记录`) rather than a pseudo execution stage, and the frontend must not reconstruct entries from raw mailbox tables or prompt tail blocks. Distribution results are backend-owned and show both delivered targets (with the propagated message) and skipped targets (with the recorded reason).
- Tree snapshots expose two visibility contracts that must not be collapsed: `parent_visible` / handshake fields are the distribution-oriented recipient projection, while browser rendering follows the browser-tree visibility fields. Execution nodes stay visible in every status; acceptance nodes stay hidden until activation. Force-showing all nodes while distribution is active is not proof that they were all valid recipients.

### Task Recovery Notice UI Contract

- 「本任务遇到异常停止，已回退到稳定步骤继续。」（`task.metadata.recovery_notice`）以全局 toast 呈现，不再是任务树内的内联气泡：打开对应任务或该任务数据刷新时弹出一次，`kind=warn`、persistent（不自动消失），标题「任务自动恢复」。
- 全局 toast 的外观合同（所有 toast 共用）：文案下方不再渲染进度条（persistent 与非 persistent 一视同仁）；toast 视口在桌面布局下以主内容区为居中基准（`left` 偏移等于侧栏宽度 160px），窄屏（≤480px，侧栏改为顶部堆叠）回落到整窗居中；垂直位置（顶部 20px + safe-area）不变。
- 用户可以点击关闭：点击 toast 任意位置（含右上角关闭按钮）即关闭，并把该任务记入本次页面会话的 dismissed 集合——同一任务不再重复弹出；切换到其他带提示的任务仍会弹出自己的提示。
- 若 toast 在用户关闭前被其他提示覆盖，下一次任务树渲染会重新弹出该提示（显示状态按“当前显示的提示文本”去重，而不是按“曾经显示过”）。
- 提示是否出现由后端元数据决定：只有非优雅中断后的恢复清洗写 `recovery_notice`；优雅暂停 + 自动恢复不产生该提示。生命周期语义见 `runtime-overview.md`「Graceful Shutdown Pause and Startup Auto-Resume」。

### Task Depth Default Contract

- The task-hall "global task tree depth" control is a global main-runtime default, backed by `PUT /api/main-runtime/settings`.
- New CEO/web sessions inherit that global default lazily. The runtime must not freeze the current global depth into ordinary session metadata just because a session was created, listed, or reopened.
- A CEO session only overrides the global task depth when the session has an explicit session-scoped override saved through `PATCH /api/ceo/sessions/{session_id}/task-defaults`.
- That explicit override is persisted as session-owned metadata and remains authoritative for later `create_async_task` calls from that session until changed again.
- Legacy session records that contain `task_defaults` without an explicit session-override marker must be treated as inherited/global, not as an override. Maintainers debugging "I changed global depth but new tasks still use an old value" should check for this distinction first.
- The practical rule is:
  - global task-hall updates should affect later new tasks immediately;
  - explicit session overrides may intentionally diverge from the global default;
  - unscoped legacy `task_defaults` must not pin later task creation to stale values.

### Heartbeat Visible-Turn Contract

- Browser-side CEO websocket payloads may carry both `inflight_turn` and `preserved_turn`.
- `inflight_turn` is the current real running turn. For heartbeat this means the heartbeat turn itself, not the earlier user bubble that is being kept on screen temporarily.
- `preserved_turn` is a live-only carryover bubble that should remain visible until a later `ceo.turn.discard` closes it.
- `preserved_turn` only exists for an older bubble that has not yet been superseded by a persisted assistant transcript entry with the same `turn_id`. Once that assistant turn is durable history, the backend/frontend should stop surfacing the preserved copy.
- Frontend rendering should treat these as two separate bubbles. It must not reuse `preserved_turn.canonical_context` as the `Interaction Flow` for the current heartbeat bubble.
- Frontend trace fallback is only safe within the same rendered turn identity. Reusing the previous bubble's trace across `turn_id` or across `source=user -> heartbeat` is a contract bug.

### CEO Session List Interaction Contract

- The session list distinguishes "session switch is still settling" (`ceoSessionBusy`, a session-view readiness flag, not a general catalog lock) from "session catalog is being mutated" (`ceoSessionCatalogBusy`, covering create / rename / delete / bulk-delete checks).
- Catalog items always carry the shape of their own session family: `china:*` / `ext:*` keys are built by the channel-shaped builder (`session_family=channel`, `channel_id`, readonly flags) in both the REST catalog and global `ceo.sessions.snapshot` / `ceo.sessions.patch` pushes; only `web:*` keys go through the local-shaped builder with the generic summary fallback. Emitting a local-shaped item for a channel key makes the browser insert that channel session into the local web-session list momentarily.
- The persisted active session id survives page refresh whenever it names any valid session: a `web:` key with a transcript/artifact, a parseable `china:` key with a transcript, or an `ext:` key that has a registry entry or a transcript. Only unknown ids fall back to the most recent local session (and write that fallback back to the state store), so resolving an active `ext:` channel session as "invalid" is a contract bug.
- The browser fetches the REST session list only on page load; live catalog updates arrive as global `ceo.sessions.snapshot` websocket pushes. The `/ws/ceo` open sequence therefore sends `hello`, the session catalog, and `ceo.state` BEFORE `snapshot.ceo`: the transcript snapshot can be tens of megabytes on long channel sessions (canonical-context payloads), and the sidebar must never be queued behind it. Frontend envelope handling is order-independent, and any reconnect re-delivers the current catalog first.
- Bulk session delete-check and delete execution are backend-owned batch contracts (`POST /api/ceo/sessions/delete-check` and `POST /api/ceo/sessions/bulk-delete`, both with `session_ids` plus one shared `delete_task_records` flag). Each returns the per-session `results[]` and the refreshed session catalog (`items`, `channel_groups`, `active_session_id`) in one payload. The frontend does not loop per-session calls for a bulk action.
- During `ceoSessionBusy` alone, the left rail still allows `new session` and bulk-selection entry so operators are not trapped in a fully disabled sidebar, though composer send/pause may stay blocked; destructive or catalog-writing actions key off the stricter mutation-safe state.

If an operator reports "switching sessions makes the whole Leader sidebar unusable", inspect these frontend flags separately before changing button rules:

1. `ceoSessionBusy`
2. `ceoSessionCatalogBusy`
3. `ceoPauseBusy`
4. `ceoUploadBusy`

Do not treat `ceoSessionBusy` as equivalent to "all session-list mutations must be locked". That coupling is a UX regression for the Leader session rail.

### Channel Session Clear Contract

- In the CEO session UI, deleting a local session removes the session record itself; deleting a channel session is a clear operation — the channel/account entry remains available, but the next reopened conversation starts from empty session context.
- Batch-delete allows mixed local and channel selections in one request; result rows distinguish `deleted=true` local removals from `cleared=true` channel clears even though the refreshed catalog arrives as one post-mutation snapshot.
- Backend clear handling for channel sessions must remove the persisted `SessionManager` transcript for that channel session key (`ext:*`, or a legacy `china:*` archive), invalidate any in-memory cached session object, and clear the same side artifacts that local-session deletion clears: inflight snapshots, paused execution context, completed continuity sidecars, uploads, and frontdoor stage-archive artifacts.
- The registry entry for an external session survives clear (get-or-create stays idempotent); only the conversation context is wiped.

If an operator reports “the channel conversation was deleted but old context came back,” inspect these layers in order:

1. `DELETE /api/ceo/sessions/{session_id}` response payload for `cleared=true`
2. persisted channel transcript files (`sessions/ext_*.jsonl`, legacy `sessions/china_*.jsonl`) and in-memory `SessionManager` cache
3. inflight / paused CEO session artifacts
4. frontend snapshot cache only after the backend-owned state is confirmed cleared

### Heartbeat/Cron Visibility Versus Prompt Inheritance

- Browser-side CEO timeline rendering and inflight bubbles are allowed to show heartbeat / cron work as ordinary active turns.
- The same heartbeat / cron round is also durable prompt history, but visibility is split in two: prompt inheritance uses `prompt_visible`, while browser transcript/snapshot rendering uses `ui_visible`.
- Maintainers should not assume "frontend cannot see the hidden rule/event bundle" means "the model cannot see it later". The hidden rule/event messages are intentionally persisted for later prompt reuse while remaining absent from UI transcript surfaces.

The current rule is:

- UI may show heartbeat / cron stage openings, tool calls, execution trace, compression state, and visible assistant replies directly.
- The hidden heartbeat / cron rule and event-bundle messages must stay out of transcript lists, session preview text, session message counts, and `snapshot.ceo.messages` by way of `ui_visible=false`.
- Later turns inherit heartbeat / cron work from the authoritative continuity baseline plus prompt-visible history, not from a separate semantic-summary-only recovery lane.

## Actual Request Debugging Contract

See `context-and-cache-troubleshooting.md`「Prompt Cache Family 与 Actual Request」 for request-artifact forensics: the split between projected input and the provider-facing request, the `actual_request_ref` / `actual_request_hash` / `actual_tool_schema_hash` fields, cache-family comparison, and runtime- vs provider-side tool-name accounting.

Web-specific artifact locations kept here:

- Every CEO/frontdoor `call_model` round writes the full provider-facing request to `.g3ku/web-ceo-requests/<session>/...json`, including internal lanes such as `token_compression` and `inline_tool_reminder`.
- Inflight / paused CEO snapshots expose only the latest `actual_request_path`, hash/count fields, and a short `actual_request_history`.
- Reopened completed sessions restore baseline state from a compact sidecar at `.g3ku/web-ceo-continuity/<session>.json`.

## Verification Pointers

Use these focused checks when validating i18n shell behavior:

- `python -m pytest tests/web/test_frontend_i18n.py -v`
- `python -m pytest tests/resources/test_bootstrap_runtime_status.py -v`

## CEO Compression UI Contract

The CEO composer has a dedicated frontdoor-compression UI path that is separate from ordinary tool progress. See `runtime-overview.md`「Frontdoor Context Compression (Current Contract)」 for the compression runtime behavior itself; this section covers only the operator-facing UI contract.

- `compression_state` only means inline frontdoor `token_compression` progress. The frontend treats `status === "running"` as "the runtime is compressing context right now" and infers no durable semantic-summary state from it.
- While running, the browser shows a left-aligned `上下文压缩中` toast near the composer (above any queued follow-up message list, not overlapping it), and pause still goes through the primary send/pause button. Clicking `暂停` during compression sends the usual `client.pause_turn`; the backend cancels compression and discards any late compression result.
- The compression toast disappears when compression finishes, errors, is discarded by pause, or the turn ends.
- Tool-wait reminder labels from the reminder sidecar are live-only event data and must not render as transcript lines, assistant bubbles, or persistent notices.

### Context Window Error UX

- If the estimated provider-bound request is already larger than the selected model's `context_window_tokens`, the frontend shows an error toast instead of attempting a semantic/global-summary fallback.
- The canonical message is `上下文大小超出当前模型<展示名>，请更改模型链配置后继续`.
- `<展示名>` is expected to come from the runtime-selected model's `provider_model`, with model `key` only as fallback.

### Composer Context Usage Meter

- The Leader composer has a live-only context-size signal: a brain-shaped usage meter beside the attachment button (not a textarea border), always visible and hoverable. It is backend-driven with two authority lanes that maintainers must keep separate:
  - Idle/non-running sessions: the browser debounces composer edits and calls `POST /api/ceo/sessions/{session_id}/composer-preflight`, whose payload represents the next outbound user batch (existing queued follow-ups plus the current unsent draft/attachments, in FIFO order). The response carries the current model-facing estimate and thresholds: `estimated_total_tokens`, `context_window_tokens`, `ratio`, `provider_model`, `trigger_tokens`, `would_trigger_token_compression`, `would_exceed_context_window`. For multimodal drafts the estimate derives image cost from a dedicated image-token heuristic plus text/schema cost — never from the raw `data:image/*;base64` string length, so a large inline data URL must not inflate into millions of text tokens.
  - Running sessions: composer-preflight stops being authoritative. The only valid source is the current `snapshot.ceo` / `ceo.turn.patch` inflight diagnostics — `frontdoor_token_preflight_diagnostics.final_request_tokens`, `.max_context_tokens`, `.provider_model`. The visible meter during a running turn means "the actual next provider-bound request the runtime is about to send", not "draft if sent now"; any browser-only fallback estimate during a running turn is a contract bug.
- With no data the base icon renders neutral with a faint glow while the colored fill stays `height 0` until an estimate arrives ("always lit" refers to the shell, not a fabricated fill). The meter exposes numbers through a styled tooltip above the icon (background, border, pop/expand on hover/focus) — not the native `title` — showing `model · estimated/total TOKEN` when an estimate exists and a waiting label otherwise; the shell keeps a matching `aria-label`.
- The brain meter is live-only UI state: it animates with the current ratio, clamps visual fill when the raw ratio exceeds `1.0`, and never creates transcript messages, assistant bubbles, or persisted snapshot entries.
- If the meter disagrees with real send-time compression/error behavior, first check whether the browser is in the idle preflight lane or the running snapshot lane, then debug the corresponding backend source. The runtime's additive image-estimation fields (`estimated_image_tokens`, `image_estimation_method`) are observability only and do not change the persisted transcript contract.

## Tool Admin RBAC Contract

Tool management uses a strict persisted-RBAC contract for surfaced tool families. See `tool-and-skill-system.md`「Tool Admin RBAC」 for the backend semantics: policy seeding, empty-list preservation and one-time repair, the `监管模式` approval switch, and `exec_runtime` execution mode.

The frontend responsibilities are:

- reflect the backend-owned `allowed_roles` exactly,
- allow all surfaced action role toggles to be unchecked,
- avoid special-casing CEO for surfaced core tool families,
- and show a clear operator-visible hint when an action is currently disabled for all roles.

If an operator reports "save succeeded but reopen restored the roles", first inspect:

1. the Tool Admin save payload,
2. the stored `tool_families.payload_json` row for that surfaced family,
3. the post-reload `GET /api/resources/tools/{tool_id}` response.

Do not start with frontdoor prompt debugging unless those three layers already agree.

If an operator reports "Tool 管理 shows one role set but runtime visibility behaves differently", inspect in this order:

1. `GET /api/resources/tools/{tool_id}`
2. the stored `tool_families.payload_json` row
3. the derived `role_policy_matrix`
4. the runtime-side `list_effective_tool_names(...)` result for the affected role/session

Do not assume the browser has a second hidden RBAC source. For surfaced tool families, the API detail payload and runtime visibility are two views over the same backend-owned family/action state.

## Container Deployment Contract

The web/admin stack has an explicit container-safe startup mode.

- `g3ku web --no-worker` is the container-safe web entrypoint.
- In this mode, the web process still owns FastAPI routes, websocket session/runtime integration, heartbeat startup, and cron startup.
- Detached task execution is expected to come from a separate `g3ku worker` process or container rather than from the web-managed local child worker path.

The default local (non-`--no-worker`) path runs a web-managed task worker with auto-restart supervision; its lease/heartbeat/stale semantics and troubleshooting are owned by `operations-and-maintenance.md`「托管 worker 看门狗」.

`/api/bootstrap/status` is also the preferred healthcheck-friendly read endpoint for the web container:

- it is available even when the project is still locked
- it reports both bootstrap mode and runtime readiness
- Compose healthchecks should use it instead of inventing a second ad-hoc web-only route

There is also a new mutable-resource startup boundary maintainers should keep in mind:

- container images may ship immutable baseline `skills/` and `tools/`
- runtime startup may seed missing baseline files into the mutable workspace copy
- that seed path must be missing-file-only and must not overwrite operator edits already present in the shared workspace volume

If operators report "the image has the new built-in skill/tool but the running project still shows the old workspace copy", debug the persistent `skills/` / `tools/` volume contents first. In container mode, the mounted workspace copy is authoritative after startup.

## Heartbeat/Cron ACK Contract

The browser handles a dedicated live-only ACK event for silent internal turns.

- `ceo.internal.ack` is emitted when a heartbeat or cron turn explicitly ends with `HEARTBEAT_OK`; it is not a normal assistant reply and must not reuse `ceo.reply.final` persistence or rendering rules. Non-silent heartbeat/cron assistant replies use the ordinary `message_end -> ceo.reply.final` path.
- The frontend renders the ACK as a distinct non-conversational bubble so operators can see the internal turn was received and intentionally stayed silent.
- That ACK bubble is ephemeral: it is not appended to the CEO session snapshot `messages` list and disappears on full refresh.
- Heartbeat `task_terminal` turns do not reach the browser as `ceo.internal.ack`; `ceo.turn.discard` still only closes a specific visible pending turn by `turn_id`.

## CEO Live Tool Reminder Contract

The CEO browser/runtime integration has a second live-only status lane for long-running direct tools: `ceo.tool.reminder`. It is intentionally different from both ordinary tool interaction steps and heartbeat turns.

- Backend reminder events are emitted only as websocket live events; the payload carries `turn_id`, `execution_id`, `tool_name`, `elapsed_seconds`, `reminder_count`, `decision`, `label`, `source="reminder"`, and an optional `terminal`.
- The frontend must not create a new assistant bubble and must not append a new interaction step for reminder events. The CEO frontend does not render `label` as a visible reminder block under the pending turn; these events stay live-only bookkeeping signals while the authoritative tool outcome arrives through the ordinary tool/error/final-reply path.

### Persistence Rules

- Reminder events are not part of `snapshot.ceo.messages` and must not be persisted into the transcript-backed CEO message list.
- Refresh/reconnect should not restore an old reminder from cached snapshot state.
- Any ephemeral reminder state is cleared when the tool finishes, the turn finalizes, the turn is discarded, or a `terminal=true` reminder event arrives.

See `heartbeat-system.md`「CEO Inline Tool Reminder Sidecar」 for the reminder decision and timeout semantics (`decision=continue` / `stop` / `unavailable`, observation-aware sidecar review, the `timeout_seconds` skip rule, and tool-call-scoped timeout-stop).

Operators should treat `ceo.tool.reminder` as a live runtime signal, not durable conversation UI; the authoritative end state still arrives through the normal CEO tool/error/final-reply events.
