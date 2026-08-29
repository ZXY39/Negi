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

- The add/edit model modal keeps one provider-config JSON draft as its source of truth. Dedicated `请求地址` (`base_url`) and `Apikey` inputs stay two-way synced with that JSON draft. In create mode `模型ID` is the binding key; in edit mode an editable `模型ID` input shows the provider model (`default_model`), so switching models keeps the binding's position in role chains.
- The protocol select (`协议`) defaults to `OpenAI Chat`; switching protocols only rewrites the draft's `provider_id` and preserves entered request address, Apikey, and parameters.
- The JSON region renders collapsed by default and auto-expands when draft validation or the connection probe fails.
- `获取模型列表` renders the provider catalog returned by the backend as a filterable list. Selecting an entry writes it into the draft's `default_model`; in create mode an empty `模型ID` is filled with the same value.
- When an edit changes `default_model` and the binding key still equals the previous `default_model` (an auto-derived key from create mode), the frontend renames the binding key to the new `default_model` via `POST /api/llm/bindings/{key}/rename`, so the displayed config name follows the model id. A key that differs from the model id (a custom name) is left unchanged.
- `测试最大并发数` is folded behind a `⋯` button next to the per-key concurrency input and requires an explicit confirmation dialog before running because the escalating probe can trigger provider rate limits.
- Per-role `最大轮数` / `最大并发数` limits live in a collapsible strip on the `模型配置` page header instead of inside the role chain cards, and the strip renders only while `编辑模型链` edit mode is active. Each limit group expands to one numeric input per role; `-1` means unlimited and is persisted as `null`, while the memory Agent `最大并发数` is fixed at 1 and renders as a non-editable pill.

### Backend Responsibilities

- `POST /api/llm/drafts/validate` and `POST /api/llm/drafts/probe` check an unsaved provider draft; `POST /api/llm/drafts/probe-max-concurrency` derives per-key concurrency limits; `POST /api/llm/drafts/models` fetches the provider model catalog (`GET {base_url}/models`) using the draft's credentials and rotates across multiple API keys on authentication failure.
- `POST /api/llm/bindings/{model_key}/rename` renames a binding key, rewrites matching references in `models.roles.*` and `agents.multi_agent.orchestrator_model_key`, and rejects an empty or duplicate key.
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

This is intentional. The composer button means "pause only when the user has not prepared a follow-up payload", not "pause whenever a turn is active".

### 2. Queued Follow-Ups

- Browser-side queued follow-ups are stored per session and rendered above the composer.
- Sending while a turn is active still does not interrupt the current turn, but the browser forwards that follow-up to the backend immediately instead of waiting for the turn to go idle first.
- The backend-owned follow-up queue is authoritative once that send succeeds. The local chip list remains a UI affordance only, and chips that were already handed off to the runtime must not be re-sent when the turn later closes.
- CEO/frontdoor consumes queued follow-ups at the safe boundary right before the next `call_model` send of the same visible turn. The runtime appends them as independent `user` messages to the current request body instead of concatenating them into one synthetic supplement string.
- If the current visible turn finishes before another `call_model` round happens, the backend immediately starts the next fresh user turn from the queued follow-ups after the current turn closes.
- Each queued item still remains its own user message in transcript persistence; batching only changes which next LLM request sees that group first.
- Once a queued follow-up has been handed off to the backend, it should still remain in the composer-side queue lane until runtime snapshot/final-reply data can prove that the follow-up has been consumed into a visible turn.
- Browser rendering must not create a provisional transcript bubble merely because a follow-up was accepted by the backend queue. Queue acceptance and visible conversation placement are intentionally different stages.
- `ceo.reply.final` may include `user_messages` for the just-finished visible turn. This is the authoritative current-turn user batch and exists specifically so the frontend can decide whether runtime-sent follow-ups belonged to that same reply or to a later chained turn.
- If `ceo.reply.final.user_messages` includes a runtime-sent follow-up, the browser must rebuild the final visible order as `current turn user batch -> final assistant reply` and clear the matched queue entry.
- If a runtime-sent follow-up does not appear in the just-finished turn's `user_messages`, the browser should keep it in the queue lane until a later fresh user turn or transcript snapshot represents it authoritatively.
- `snapshot.ceo.messages` must also avoid replaying running-turn `pending` user transcript rows as ordinary history bubbles. During a live running turn, authoritative current-turn user placement comes from `inflight_turn.user_messages`, not from flat transcript replay.
- When a running follow-up is actually consumed into the next model send of the same visible conversation lane, the runtime also archives the pre-follow-up assistant execution bubble into visible UI history before the consumed user bubble is restored. That archive is UI-visible but prompt-hidden, so refresh/reconnect can preserve the same visual ordering without polluting later prompt history.

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
- `text` is the authoritative assistant text of the latest thinking segment of the current visible turn at that stream point. The previous segment stays visible until a new one arrives — a model-call boundary resets the segment and the first streamed delta of the new call replaces it — so the live block shows only the latest segment instead of a whole-turn accumulation; it is not a raw provider token and not a stage-trace snapshot.
- The browser should treat `ceo.reply.delta` as a cheap text-only lane:
  - render the latest-segment text as a transient live block inside the turn's stage rail (`.task-trace-live-text`), not into the final assistant bubble; the bubble only receives the authoritative final text at `ceo.reply.final`
  - update cached `inflight_turn.assistant_text` for reconnect/session restore
  - never treat the delta as durable transcript history and never rerender the stage trace from it
  - do not rerender stage trace
  - do not treat the delta as durable transcript history
- Streamed reply rendering should stay in a plain-text path while the turn is still running. The full markdown render path is reserved for `ceo.reply.final`.
- `ceo.reply.final` remains the authoritative closeout event for the assistant bubble. Final markdown rendering, transcript finalization, canonical-context finalization, and visible-turn completion still happen there.
- The CEO turn body is rendered as a stage timeline reconstructed from the turn's canonical context / stage state, not as a single overwritten bubble: each stage renders its `preamble_text` (the narration that accompanied its creation) above the stage header, collapsed by default to stage titles with budget/progress meta; expanding a stage reveals per-round blocks carrying `rounds[].text` (the model's mid-turn narration) plus that round's tool cards (clickable for arguments/output, externalized refs resolved through the existing content API). The final output renders below the stage rail and user messages render right-aligned. The historical separate "Interaction Flow" details wrapper is retired for the CEO feed; rounds with no mid-turn text render tool cards only.
- Because the timeline reads stage/round records, mid-turn narration survives page reloads via the persisted `canonical_context` on assistant transcript entries even though the transcript content itself stores only the final reply text.
- `ceo.turn.patch` is not the high-frequency assistant-text streaming lane; it is the lower-frequency lane for inflight/preserved snapshot refreshes, state transitions, reconnect bootstrap, and tool/interrupt-related snapshot changes.

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
- The same persisted `metadata.web_ceo_uploads` lane is also the source-of-truth for rebuilding `attachment_reopen_targets` in later frontdoor turns. If attachment bubbles still restore correctly but later runtime contracts lose reopen paths, debug transcript metadata preservation before debugging contract rendering.
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

- Manual pause means “freeze the current round as previous-round context,” and the backend does not leave ordinary user sessions in a long-lived resumable `paused` state.
- The operator-visible “pause” button is a terminal stop for the current visible turn: the backend ends the runtime state as `completed` and tags it with `stop_reason=user_pause`.
- The next outbound user message after pause must start a new round.
- The paused round's user message, execution trace, stage state, tool calls, and compression state are preserved in transcript and snapshot context so the next round can inherit them without rewriting the original user text.
- Manual pause writes a completed-session continuity sidecar immediately, then clears the ordinary paused/inflight restorable snapshots for that session.
- The backend archives the previous paused assistant bubble into a persisted assistant message with `status=paused` during the stop flow itself, not only when the next user turn is about to dispatch.
- That archived paused assistant is durable UI history for `snapshot.ceo` restore/reconnect, but it remains hidden from prompt-history assembly and session-summary counts via `history_visible=false`.
- Browser-side restore should therefore render that persisted paused assistant as a paused bubble rather than a completed reply. The next ordinary user turn inherits context from visible history plus completed continuity state, not from resuming the old paused turn.

### CEO Stage Trace Round Rendering Contract

- The browser CEO stage view should treat `canonical_context.stages[].rounds[].tools` as the authoritative round-level tool list.
- Refreshing the page or reopening a completed session should reproduce the same round grouping that live inflight snapshots used; the frontend should not try to regroup same-name tools on its own.
- `tool_names` and `tool_call_ids` may still be present for compatibility, but they are summary metadata rather than a second grouping algorithm.
- The stage progress badge in both the CEO session view and the shared task-trace components must reflect budget-counted rounds rather than raw round history length.
- Frontend progress rendering should use `tool_rounds_used` as the primary source, and only infer a fallback count from `rounds[].budget_counted=true` when an older payload lacks an explicit count.
- Do not derive displayed progress from plain `rounds.length`: successful `load_tool_context` / `load_skill_context` rounds may remain in history for auditability while being hidden from visible execution chips, and treating raw round count as budget usage will overstate progress.

### CEO Turn Timeline Rendering Contract

- The CEO session view renders each visible turn as a timeline: user message, then the turn's own stage rail (stage titles collapsed by default, expandable to rounds), then the final assistant output. The older "single bubble plus collapsible Interaction Flow wrapper" layout has been replaced; the wrapper element remains only as the rail container.
- Live inflight turns must resolve stage data from the per-turn `canonical_context_delta` first, then from the turn's own already-rendered trace summary. They must never fall back to the session-cumulative `canonical_context` on the inflight lane: `_frontdoor_stage_state` accumulates across turns at session level, so a full-context fallback leaks the previous turn's stages into a new live turn. When a patch carries a different `turn_id` than the reused turn, the browser clears that turn's rendered trace summary and live stream text before resolving context.
- Persisted assistant messages keep `canonical_context` (cumulative) and `canonical_context_delta` (relative to the previous assistant message); refresh and reconnect render the delta so each turn shows only its own stages. `preserved_turn` snapshots may still fall back to full context because follow-up archival can absorb their delta baseline.
- `rounds[].text` carries the model's mid-turn narration for that tool round; a stage's `preamble_text` carries the narration from the response that created the stage (rendered above the stage title). Both are display-only fields of the stage state; prompt assembly, transcript durability, and archive semantics do not depend on them.
- `ceo.reply.final` remains the authoritative closeout (final markdown plus final delta/trace when present). If the completed turn produced no stage trace, the final payload omits `canonical_context` and the browser must not backfill an older trace under the new bubble.
- The frontend must not reconstruct CEO stage flow from flat `tool_events`; `canonical_context_delta` is computed server-side from message order, so refresh/reconnect rebuild the same per-bubble trace slices without a browser-local cursor.
- Tool output rendering follows the canonical payload directly: a tool entry with `output_text` shows that inline full text, while a tool entry with only `output_ref` shows the preview text and keeps the artifact-open path for the full body. The frontend must not invent extra truncation or backfill old tool-event text when canonical context is present.

The backend contract behind that UI behavior is:

- CEO/frontdoor runtime writes precise round tool entries into `frontdoor_stage_state.stages[].rounds[].tools` when a tool cycle finishes.
- Session snapshot assembly trusts stored `round.tools` first and only backfills legacy rounds by exact `tool_call_id`.
- A `tool_name`-only fallback is considered a regression because it can make a later same-name tool appear inside an earlier stage round after refresh or transcript reload.
- The browser still treats `round.tools` as authoritative input, but it filters successful `load_tool_context` / `load_skill_context` entries out of the visible stage-trace tool chips because those calls represent context acquisition rather than user-facing execution work.
- `ceo.reply.final` carries the authoritative final `canonical_context` when the completed turn has stage data; browsers should prefer that payload instead of reusing an older inflight snapshot.
- If the current turn never produced a stage trace, `ceo.reply.final` must omit `canonical_context` entirely rather than backfilling the previous persisted assistant trace. Reusing an older trace under a new direct-reply bubble is a frontend/backend contract bug.
- Empty-delta rule: when the completed turn produced **no new stage progress** relative to the previous assistant message (i.e. the computed `canonical_context_delta` is empty), the final payload omits **both** `canonical_context` and `canonical_context_delta`. The shared final-payload assembler `final_reply_canonical_merge` (`g3ku/runtime/web_ceo_sessions.py`) enforces this in the userspace relay and the heartbeat publisher alike. On the browser, a message whose `canonical_context_delta` key is present but renders empty stages must render the plain text bubble with **no** stage rail, and `finalizeCeoTurn` must not fall back to `turn.lastExecutionTraceSummary` or to the session-cumulative `canonical_context`. The same rule applies to `renderPersistedCeoAssistantTurn` so refresh, reconnect, and in-memory session-cache replay stay identical to live.

### Heartbeat Compatibility

- Heartbeat turns count as active session work for the composer button and queueing logic.
- Queued follow-ups should not interrupt heartbeat execution.
- Active-turn follow-ups are forwarded to the backend during heartbeat execution as well. They may be merged into the next safe `call_model` boundary of that same running turn, or become the next fresh user batch immediately after heartbeat finishes if no same-turn model round remains.
- Terminal-event delivery is idempotent per session: the heartbeat service records the `dedupe_key` of every `task_terminal` event for which a user-visible reply was persisted (`session.metadata["handled_terminal_dedupe_keys"]`, bounded and normalized). If the same terminal `dedupe_key` is delivered again (e.g. because a previous run persisted the reply but was interrupted before the outbox ack / queue pop), the re-delivery is acknowledged silently — popped from the queue and marked delivered — **without** running the agent or emitting a second `ceo.reply.final`. A fresh terminal for a different completion (`task_id`/`status`/`finished_at`) has a distinct `dedupe_key` and still produces its own reply. The `dedupe_key` itself is always recomputed server-side as the canonical `task-terminal:{task_id}:{status}:{finished_at}` — caller-supplied keys are ignored so variant keys cannot force a duplicate delivery. See `heartbeat-system.md`「Task Terminal Repair Contract」.
- Consequence: in the rare window where a run persists the reply but crashes before `ceo.reply.final` is actually published, the duplicate delivery is suppressed and the user sees the reply from the transcript snapshot (refresh / reconnect / next session sync) rather than a live push. The transcript remains authoritative.

### Task Hall Action Contract

- The browser task hall only exposes `pause`, `resume`, and `delete` task actions.
- `retry`, `continue-evaluate`, and `open continuation` actions are removed from both the UI flow and the REST surface.
- Task list and task detail status pills derive from the current task `status` plus final-acceptance state; legacy continuation metadata fields are ignored even if older task records still carry them.
- The task-hall multi-select `选择` menu has six backend-aligned buckets: `已暂停` / `完成` / `未读` / `失败` / `未通过` / `进行中`.
- `完成` means strict `taskStatusKey(task) === "success"`, while `未通过` means strict `taskStatusKey(task) === "unpassed"`. Maintainers must not fold `unpassed` into the failed bucket just because final acceptance ended in a business rejection.
- Task-hall batch delete is a backend-owned contract: the browser sends one `POST /api/tasks/bulk-delete` request with `task_ids` instead of fanning out one `DELETE /api/tasks/{task_id}` call per selected row.
- The batch-delete response is per-task, not all-or-nothing. Frontend code should interpret each returned `items[]` row's `result` (`deleted`, `not_found`, `failed`) before choosing success/warn/error toast behavior.

### Task Message Distribution UI Contract

- Task-tree distribution UI is task-wide, not root-only: the frontend treats `runtime_summary.distribution.mode == "task_wide_barrier"` as the authoritative banner source instead of inferring state from the root node's pending-notice count alone.
- While the task is in `barrier_requested`, `barrier_draining`, or `distributing`, the task-tree view shows a task-local sticky notice and the execution-tree wrapper switches connector lines into a yellow distribution mode. This is task-scoped state, not a global shell toast; the sticky notice disappears once the target node exposes the pending message through its backend-owned message list / `pending_notice_count`, and a node-level `distribution_status` of `barrier_blocked` renders as a yellow warning.
- Node detail receives a backend-owned message list (its own section before `派生记录`) rather than a pseudo execution stage, and the frontend must not reconstruct entries from raw mailbox tables or prompt tail blocks. Distribution results are backend-owned and show both delivered targets (with the propagated message) and skipped targets (with the recorded reason).
- Tree snapshots expose two visibility contracts that must not be collapsed: `parent_visible` / handshake fields are the distribution-oriented recipient projection, while browser rendering follows the browser-tree visibility fields. Execution nodes stay visible in every status; acceptance nodes stay hidden until activation. Force-showing all nodes while distribution is active is not proof that they were all valid recipients.

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
- Bulk session delete-check and delete execution are backend-owned batch contracts (`POST /api/ceo/sessions/delete-check` and `POST /api/ceo/sessions/bulk-delete`, both with `session_ids` plus one shared `delete_task_records` flag). Each returns the per-session `results[]` and the refreshed session catalog (`items`, `channel_groups`, `active_session_id`) in one payload. The frontend does not loop per-session calls for a bulk action.
- During `ceoSessionBusy` alone, the left rail still allows `new session` and bulk-selection entry so operators are not trapped in a fully disabled sidebar, though composer send/pause may stay blocked; destructive or catalog-writing actions key off the stricter mutation-safe state.

If an operator reports "switching sessions makes the whole Leader sidebar unusable", inspect these frontend flags separately before changing button rules:

1. `ceoSessionBusy`
2. `ceoSessionCatalogBusy`
3. `ceoPauseBusy`
4. `ceoUploadBusy`

Do not treat `ceoSessionBusy` as equivalent to "all session-list mutations must be locked". That coupling is a UX regression for the Leader session rail.

### Channel Session Clear Contract

- In the CEO session UI, deleting a local session and deleting a channel session are intentionally different operations.
- Deleting a local session removes the session record itself. Deleting a channel session is a clear operation: the channel/account entry remains available, but the next reopened conversation must start from empty session context.
- In the batch-delete contract, mixed local and channel selections are allowed in one request. Result rows therefore distinguish `deleted=true` local removals from `cleared=true` channel clears even though the refreshed catalog still arrives as one post-mutation snapshot.
- Backend clear handling for channel sessions must remove the persisted `SessionManager` transcript for that `china:*` session key, invalidate any in-memory cached session object, and clear the same side artifacts that local-session deletion clears for that session id, including inflight snapshots, paused execution context, completed continuity sidecars, uploads, and frontdoor stage-archive artifacts.
- Both local-session delete and channel-session clear also ask the runtime to purge SQLite checkpointer rows for that exact session key. The purge runs as a background task after the HTTP response, so the delete returns immediately while checkpointer rows clear moments later. If the transcript is gone but old state still appears to resurrect after reopen, inspect the checkpointer purge path before blaming frontend cache.
- For DM channel rows, the catalog entry may still remain visible after clear because it is synthesized from enabled channel-account configuration rather than from transcript persistence alone.

If an operator reports “the channel conversation was deleted but old context came back,” inspect these layers in order:

1. `DELETE /api/ceo/sessions/{session_id}` response payload for `cleared=true`
2. persisted `sessions/china_*.jsonl` transcript files and in-memory `SessionManager` cache
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

- `compression_state` only means inline frontdoor `token_compression` progress. The frontend should treat `status === "running"` as "the runtime is compressing context right now" and should not infer any durable semantic-summary state from it.
- While `compression_state.status === "running"`, the browser shows the `上下文压缩中` toast near the composer, but pause still goes through the existing primary send/pause button at the right side of the input row.
- The compression toast is intentionally left-aligned within the composer flow instead of floating over the right-side loader lane.
- When queued follow-up messages are present, the compression toast must render above that queued-message list rather than overlapping it.
- Clicking the ordinary primary `暂停` button during compression still sends the usual `client.pause_turn` request; the backend is responsible for cancelling compression and discarding any late compression result.
- When compression finishes, errors, is discarded by pause, or the turn ends, the compression toast must disappear.
- Tool-wait reminder labels from the reminder sidecar are live-only event data and should remain hidden from the visible CEO feed. They must not render as transcript lines, assistant bubbles, or persistent notices.

### Context Window Error UX

- If the estimated provider-bound request is already larger than the selected model's `context_window_tokens`, the frontend shows an error toast instead of attempting a semantic/global-summary fallback.
- The canonical message is `上下文大小超出当前模型<展示名>，请更改模型链配置后继续`.
- `<展示名>` is expected to come from the runtime-selected model's `provider_model`, with model `key` only as fallback.

### Composer Context Usage Meter

- The Leader composer has a second live-only context-size signal: a brain-shaped usage meter beside the attachment button, not a border around the textarea.
- That meter is backend-driven rather than a frontend-only guess, but it has two distinct authority lanes that maintainers must keep separate.
- For idle/non-running sessions, the browser may debounce composer edits and call `POST /api/ceo/sessions/{session_id}/composer-preflight`.
- That preflight request payload should represent the next outbound user batch for that session: existing queued follow-ups plus the current unsent draft/attachments, in FIFO order.
- The preflight response should include the current model-facing estimate and threshold fields, including `estimated_total_tokens`, `context_window_tokens`, `ratio`, `provider_model`, `trigger_tokens`, `would_trigger_token_compression`, and `would_exceed_context_window`.
- For multimodal drafts, that estimate must not scale with the raw `data:image/...;base64,...` string length. Backend preflight derives image cost from a dedicated image-token heuristic plus text/schema cost, so a large inline data URL should not look like millions of text tokens just because it serialized to a huge JSON string.
- For running sessions, the browser must stop treating composer-preflight as authoritative. The only valid source is the current inflight turn snapshot from `snapshot.ceo` / `ceo.turn.patch`, specifically the latest `frontdoor_token_preflight_diagnostics.final_request_tokens`, `frontdoor_token_preflight_diagnostics.max_context_tokens`, and `frontdoor_token_preflight_diagnostics.provider_model`.
- This means the visible meter during a running turn is not "draft if sent now"; it is "the actual next provider-bound request the runtime is about to send."
- When the current inflight snapshot does not yet carry an exact runtime request estimate, the meter must stay visually empty. Frontend code must not show `pending`, must not reuse the previous composer estimate, and must not infer a replacement value from the draft textarea, pinned sent entries, or `inflight_turn.user_message`.
- The brain meter itself is live-only UI state. It must animate with the current ratio, clamp visual fill when the raw ratio exceeds `1.0`, and never create transcript messages, assistant bubbles, or persisted snapshot entries.
- If the meter appears inconsistent with real send-time compression/error behavior, first check whether the browser is in the idle preflight lane or the running snapshot lane, then debug the corresponding backend source. Treat any browser-only fallback estimate during a running turn as a contract bug.
- The runtime diagnostics for that meter also expose additive image-estimation fields such as `estimated_image_tokens` and `image_estimation_method`. These are observability fields for operators and maintainers; they do not change the persisted transcript contract.

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
- In this mode, the web process still owns FastAPI routes, websocket session/runtime integration, heartbeat startup, cron startup, and China bridge supervision.
- Detached task execution is expected to come from a separate `g3ku worker` process or container rather than from the web-managed local child worker path.

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
