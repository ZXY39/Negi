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

## Memory Management Page And Admin Contract

The browser shell now has a top-level `记忆管理` page. This is intentionally a read-only operator surface for the queued long-term memory runtime.

### Frontend Responsibilities

- The left rail exposes `记忆管理` as its own top-level navigation item, not as a nested subsection of model configuration.
- The page shows two independent columns:
  - unprocessed queue items, oldest-first, in real queue order
  - terminal processed batches, newest-first, including both applied and discarded outcomes
- Queue cards default to collapsed and show runtime-owned state such as `pending` / `processing`, enqueue time, and the latest engineering error when present.
- Processed cards default to collapsed and show batch-owned metadata such as processed time, terminal status, op type, token usage, model chain, attempts, discard reason when present, and note refs.
- Memory list cards are intentionally compact. Clicking a queue or processed card opens a frontend-owned read-only detail modal for the full payload instead of expanding the entire text inline inside the list.
- The detail modal keeps long payloads inside scrollable text regions so one large row does not push the rest of the list off-screen.
- Queue / processed detail text still treats `ref:note_xxxx` as a read-only preview trigger. Clicking a note ref opens a second frontend-owned drawer/modal that only fetches and displays the note body; it must not expose edit or save controls.
- Page-level memory errors and queue-head blocked notices now use the shared app toast style instead of persistent inline banners under the memory page header.
- The page is read-only in v1. There are no browser buttons for retry, delete, edit, or force-flush.

### Backend Responsibilities

- `GET /api/memory/queue` returns queue-owned runtime state directly from `memory/queue.jsonl`, including `status`, retry/error fields, and pagination metadata.
- `GET /api/memory/processed` returns terminal batch records from `memory/ops.jsonl`, including both applied rows and durable discarded rows, also with pagination metadata.
- `GET /api/memory/notes/{ref}` is the minimal read-only note preview contract for the memory page. It returns the note body for an existing `ref:note_xxxx` entry and returns a clear not-found error when the referenced note file is missing.
- `POST /api/memory/admin/retry-head` exists as a guarded operator contract, but it is disabled by default. When `G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS` is not enabled, the backend returns `403` with `detail.code=memory_admin_mutation_disabled`; successful calls append an audit record to `memory/admin_audit.jsonl`.
- Older admin memory endpoints such as dense-index reset/rebuild and runtime stats still exist, but they are no longer the primary operator path for understanding whether long-term memory is healthy.

### Maintenance Boundary

- If the memory page looks wrong but the raw JSON from `/api/memory/queue` or `/api/memory/processed` is correct, debug `g3ku/web/frontend/*`.
- If a `ref:note_xxxx` chip renders but the preview drawer cannot load, compare the frontend `ApiClient.getMemoryNote(...)` request with `GET /api/memory/notes/{ref}` before debugging the memory runtime itself.
- If the page is missing fields, ordering, discarded statuses, or request-artifact links already in the API response, debug the admin endpoints or `g3ku/agent/memory_agent_runtime.py`.
- If the queue page is stuck on one `processing` batch, treat that as a backend/runtime issue first, not as a frontend pagination bug.
- Browser-side memory management remains read-only by default. If an operator expects a retry button in the UI, first check whether the feature was intentionally kept backend-only for the current build rather than debugging missing DOM wiring.

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

The Leader/CEO composer now has two distinct runtime behaviors that maintainers need to keep straight.

### 1. Active-Turn Button Semantics

- If the current session is idle and the composer is empty, the primary button stays in a disabled `send` state.
- If a user-visible turn is currently running and the composer is empty, the primary button switches to `pause`.
- If the composer contains text or attachments, the primary button switches back to `send` even when a user turn or heartbeat turn is still running.

This is intentional. The composer button no longer means "pause whenever a turn is active"; it means "pause only when the user has not prepared a follow-up payload".

### 2. Queued Follow-Ups

- Browser-side queued follow-ups are stored per session and rendered above the composer.
- Sending while a turn is active still does not interrupt the current turn, but the browser now forwards that follow-up to the backend immediately instead of waiting for the turn to go idle first.
- The backend-owned follow-up queue is authoritative once that send succeeds. The local chip list remains a UI affordance only, and chips that were already handed off to the runtime must not be re-sent when the turn later closes.
- CEO/frontdoor now consumes queued follow-ups at the safe boundary right before the next `call_model` send of the same visible turn. The runtime appends them as independent `user` messages to the current request body instead of concatenating them into one synthetic supplement string.
- If the current visible turn finishes before another `call_model` round happens, the backend immediately starts the next fresh user turn from the queued follow-ups after the current turn closes.
- Each queued item still remains its own user message in transcript persistence; batching only changes which next LLM request sees that group first.

### 2.5. Image Upload Gating

- Web CEO uploads still persist attachment metadata plus the text note that exposes the local file path to the agent.
- The websocket/input layer no longer unconditionally turns uploaded images into provider-visible `image_url` blocks.
- CEO/frontdoor now decides per turn whether uploaded images should expand into multimodal request content. The decision key is the selected model binding's `image_multimodal_enabled` flag.
- If `image_multimodal_enabled=false`, uploads stay on the text downgrade path and the model only sees ordinary text plus the attachment note.
- If `image_multimodal_enabled=true`, only the live request of the current visible turn may contain provider-visible image input. Durable transcript/baseline lanes still strip those image blocks back out.
- Web CEO upload protection now has two layers:
  - `/api/ceo/uploads` rejects any single image larger than `5 MiB`
  - the runtime rechecks image size again before expanding the upload into a provider request, so bypassing the upload endpoint does not bypass the limit
- Composer/preflight estimation must use the same expansion rule as the real send. If the current model binding would not expand images, the meter/preflight must estimate the downgraded text-only request instead of pretending an image will be sent.

### 3. Context Loader Notices

- Successful CEO/frontdoor `load_tool_context` and `load_skill_context` calls are no longer shown as ordinary `Interaction Flow` steps under the assistant bubble.
- Frontend loader-notice detection must treat both legacy and v2 loader names as the same UI family:
  - `load_tool_context` / `load_tool_context_v2` => tool notice
  - `load_skill_context` / `load_skill_context_v2` => skill notice
- Instead, the browser shows a short-lived composer notice above the input row, using the loaded `tool_id` or `skill_id` when the runtime payload exposes it.
- These notices are intentionally stackable rather than single-slot: multiple successful loader calls may coexist in one right-aligned floating column that lines up with the send-button edge.
- The type-specific styling now comes from a leading icon instead of the older leading green dot:
  - tool notices use the same `wrench` icon family as the sidebar Tool page
  - skill notices use the same `sparkles` icon family as the sidebar Skill page
- The risk-colored dot remains present on the trailing edge so operators can still distinguish low / medium / high loader risk at a glance.
- The intended motion contract is still "launch from the composer, settle into the notice stack, then fade out"; the full lifecycle is currently about 5 seconds per notice.
- That notice is intentionally live-only UI state. It should fade away after a short timeout and must not be appended into the persisted CEO session `messages` list.

### Manual Pause Resume Rule

- Manual pause still means “freeze the current round as previous-round context,” but the backend no longer leaves ordinary user sessions in a long-lived resumable `paused` state.
- The operator-visible “pause” button is now a terminal stop for the current visible turn: the backend ends the runtime state as `completed` and tags it with `stop_reason=user_pause`.
- The next outbound user message after pause must start a new round.
- The paused round's user message, execution trace, stage state, tool calls, and compression state are preserved in transcript and snapshot context so the next round can inherit them without rewriting the original user text.
- Manual pause now writes a completed-session continuity sidecar immediately, then clears the ordinary paused/inflight restorable snapshots for that session.
- The backend now archives the previous paused assistant bubble into a persisted assistant message with `status=paused` during the stop flow itself, not only when the next user turn is about to dispatch.
- That archived paused assistant is durable UI history for `snapshot.ceo` restore/reconnect, but it remains hidden from prompt-history assembly and session-summary counts via `history_visible=false`.
- Browser-side restore should therefore render that persisted paused assistant as a paused bubble rather than a completed reply. The next ordinary user turn inherits context from visible history plus completed continuity state, not from resuming the old paused turn.

### CEO Stage Trace Round Rendering Contract

- The browser CEO stage view should treat `canonical_context.stages[].rounds[].tools` as the authoritative round-level tool list.
- Refreshing the page or reopening a completed session should reproduce the same round grouping that live inflight snapshots used; the frontend should not try to regroup same-name tools on its own.
- `tool_names` and `tool_call_ids` may still be present for compatibility, but they are summary metadata rather than a second grouping algorithm.
- The stage progress badge in both the CEO session view and the shared task-trace components must reflect budget-counted rounds rather than raw round history length.
- Frontend progress rendering should use `tool_rounds_used` as the primary source, and only infer a fallback count from `rounds[].budget_counted=true` when an older payload lacks an explicit count.
- Do not derive displayed progress from plain `rounds.length`: successful `load_tool_context` / `load_skill_context` rounds may remain in history for auditability while being hidden from visible execution chips, and treating raw round count as budget usage will overstate progress.

The backend contract behind that UI behavior is:

- CEO/frontdoor runtime writes precise round tool entries into `frontdoor_stage_state.stages[].rounds[].tools` when a tool cycle finishes.
- Session snapshot assembly trusts stored `round.tools` first and only backfills legacy rounds by exact `tool_call_id`.
- A `tool_name`-only fallback is considered a regression because it can make a later same-name tool appear inside an earlier stage round after refresh or transcript reload.
- The browser still treats `round.tools` as authoritative input, but it filters successful `load_tool_context` / `load_skill_context` entries out of the visible stage-trace tool chips because those calls represent context acquisition rather than user-facing execution work.
- `ceo.reply.final` now carries the authoritative final `canonical_context` when the completed turn has stage data; browsers should prefer that payload instead of reusing an older inflight snapshot.
- If the current turn never produced a stage trace, `ceo.reply.final` must omit `canonical_context` entirely rather than backfilling the previous persisted assistant trace. Reusing an older trace under a new direct-reply bubble is a frontend/backend contract bug.

### Heartbeat Compatibility

- Heartbeat turns count as active session work for the composer button and queueing logic.
- Queued follow-ups should not interrupt heartbeat execution.
- Active-turn follow-ups are now forwarded to the backend during heartbeat execution as well. They may be merged into the next safe `call_model` boundary of that same running turn, or become the next fresh user batch immediately after heartbeat finishes if no same-turn model round remains.

### Task Hall Action Contract

- The browser task hall now only exposes `pause`, `resume`, and `delete` task actions.
- `retry`, `continue-evaluate`, and `open continuation` actions are removed from both the UI flow and the REST surface.
- Task list and task detail status pills now derive from the current task `status` plus final-acceptance state; legacy continuation metadata fields are ignored even if older task records still carry them.

### Task Message Distribution UI Contract

- When the current task enters message distribution mode (`runtime_summary.distribution.active_epoch_id/state` is present), the task-tree view now surfaces a task-local sticky notice above the tree with text `接收到新消息，分发中`.
- The same sticky notice now has a pending-notice fallback after the compact distribution window closes: if the root node snapshot still reports unconsumed appended messages, the tree keeps a yellow notice in place with text equivalent to `接收到新消息，等待节点处理` until the node consumes them.
- During the same distribution window, the execution-tree wrapper switches into a dedicated distribution visual mode and forces all connector lines into the same yellow family, regardless of the individual node success/running/failed color mapping.
- This is intentionally task-scoped UI state, not a global shell toast. It should follow the currently opened task detail view and disappear only after both distribution state and root pending-notice state clear.
- Node detail now receives a backend-owned message list contract instead of rendering appended messages as a pseudo execution stage. The frontend must not reconstruct these entries from raw mailbox tables or by parsing prompt tail blocks.
- That same rule applies to root-node appended messages: the backend may expose them through the node detail message list even when the root has no `task_node_notifications` row, because root delivery uses node-local pending notice metadata rather than mailbox storage.
- In the node detail drawer, the message list appears as its own section before `派生记录`. Each entry shows received time plus pending/consumed state, expands to the full message body, and includes the per-node distribution result (the child messages sent during that epoch/source turn, or `无` when no child delivery happened).

### Task Depth Default Contract

- The task-hall "global task tree depth" control is a global main-runtime default, backed by `PUT /api/main-runtime/settings`.
- New CEO/web sessions now inherit that global default lazily. The runtime must not freeze the current global depth into ordinary session metadata just because a session was created, listed, or reopened.
- A CEO session only overrides the global task depth when the session has an explicit session-scoped override saved through `PATCH /api/ceo/sessions/{session_id}/task-defaults`.
- That explicit override is persisted as session-owned metadata and remains authoritative for later `create_async_task` calls from that session until changed again.
- Legacy session records that contain `task_defaults` without an explicit session-override marker must be treated as inherited/global, not as an override. Maintainers debugging "I changed global depth but new tasks still use an old value" should check for this distinction first.
- The practical rule is:
  - global task-hall updates should affect later new tasks immediately;
  - explicit session overrides may intentionally diverge from the global default;
  - unscoped legacy `task_defaults` should no longer pin later task creation to stale values.

### Heartbeat Visible-Turn Contract

- Browser-side CEO websocket payloads may now carry both `inflight_turn` and `preserved_turn`.
- `inflight_turn` is the current real running turn. For heartbeat this means the heartbeat turn itself, not the earlier user bubble that is being kept on screen temporarily.
- `preserved_turn` is a live-only carryover bubble that should remain visible until a later `ceo.turn.discard` closes it.
- `preserved_turn` only exists for an older bubble that has not yet been superseded by a persisted assistant transcript entry with the same `turn_id`. Once that assistant turn is durable history, the backend/frontend should stop surfacing the preserved copy.
- Frontend rendering should treat these as two separate bubbles. It must not reuse `preserved_turn.canonical_context` as the `Interaction Flow` for the current heartbeat bubble.
- Frontend trace fallback is only safe within the same rendered turn identity. Reusing the previous bubble's trace across `turn_id` or across `source=user -> heartbeat` is a contract bug.

### CEO Session List Interaction Contract

- The session list distinguishes between "session switch is still settling" and "session catalog is being mutated".
- Frontend `ceoSessionBusy` means the active-session switch is still waiting for the new CEO websocket snapshot / connection state to settle. This is a session-view readiness flag, not a general catalog lock.
- Frontend `ceoSessionCatalogBusy` means the session catalog itself is being refreshed or mutated by create / rename / delete / bulk-delete checks.

The intended operator-visible behavior is:

- During `ceoSessionBusy` alone, the left rail should still allow `new session` and bulk-selection entry/selection so operators do not get trapped in a fully disabled sidebar after switching sessions.
- During `ceoSessionBusy`, composer send/pause and any action that depends on the active session being fully ready may still remain blocked.
- Destructive or catalog-writing actions such as rename, delete, and bulk delete should continue to key off the stricter mutation-safe state rather than the relaxed selection state.
- During `ceoSessionCatalogBusy`, pause requests, or attachment uploads, the left rail may still disable new-session creation and bulk-selection controls because those operations are competing with in-flight catalog or payload changes.

If an operator reports "switching sessions makes the whole Leader sidebar unusable", inspect these frontend flags separately before changing button rules:

1. `ceoSessionBusy`
2. `ceoSessionCatalogBusy`
3. `ceoPauseBusy`
4. `ceoUploadBusy`

Do not treat `ceoSessionBusy` as equivalent to "all session-list mutations must be locked". That coupling is a UX regression for the Leader session rail.

### Channel Session Clear Contract

- In the CEO session UI, deleting a local session and deleting a channel session are intentionally different operations.
- Deleting a local session removes the session record itself. Deleting a channel session is a clear operation: the channel/account entry remains available, but the next reopened conversation must start from empty session context.
- Backend clear handling for channel sessions must remove the persisted `SessionManager` transcript for that `china:*` session key, invalidate any in-memory cached session object, and clear the same side artifacts that local-session deletion clears for that session id, including inflight snapshots, paused execution context, completed continuity sidecars, uploads, and frontdoor stage-archive artifacts.
- For DM channel rows, the catalog entry may still remain visible after clear because it is synthesized from enabled channel-account configuration rather than from transcript persistence alone.

If an operator reports “the channel conversation was deleted but old context came back,” inspect these layers in order:

1. `DELETE /api/ceo/sessions/{session_id}` response payload for `cleared=true`
2. persisted `sessions/china_*.jsonl` transcript files and in-memory `SessionManager` cache
3. inflight / paused CEO session artifacts
4. frontend snapshot cache only after the backend-owned state is confirmed cleared

### Heartbeat/Cron Visibility Versus Prompt Inheritance

- Browser-side CEO timeline rendering and inflight bubbles are allowed to show heartbeat / cron work as ordinary active turns.
- The same heartbeat / cron round is also durable prompt history now, but visibility is split in two: prompt inheritance uses `prompt_visible`, while browser transcript/snapshot rendering uses `ui_visible`.
- Maintainers should not assume "frontend cannot see the hidden rule/event bundle" means "the model cannot see it later". The hidden rule/event messages are intentionally persisted for later prompt reuse while remaining absent from UI transcript surfaces.

The current rule is:

- UI may show heartbeat / cron stage openings, tool calls, execution trace, compression state, and visible assistant replies directly.
- The hidden heartbeat / cron rule and event-bundle messages must stay out of transcript lists, session preview text, session message counts, and `snapshot.ceo.messages` by way of `ui_visible=false`.
- Later turns inherit heartbeat / cron work from the authoritative continuity baseline plus prompt-visible history, not from a separate semantic-summary-only recovery lane.

## Actual Request Debugging Contract

Node detail and latest-context views now expose two different debugging surfaces that maintainers must not mix up.

- The existing node input / projected context view is still a projection-oriented operator surface. It is useful for understanding durable state and task intent, but it is not guaranteed to be the exact request body sent to the provider.
- The actual provider request is represented separately through `actual_request_ref`, `actual_request_hash`, `actual_request_message_count`, and `actual_tool_schema_hash`.
- For node runtime specifically, `actual_request_ref` is now a dedicated per-`call_model` artifact, not a reuse of runtime `messages_ref`. The artifact is the authority for request-forensics; projected input and runtime-frame messages remain separate lenses.
- That node actual-request artifact stores both the runtime-side projection (`model_messages`, `request_messages`, `actual_tool_schemas`, cache-family hashes) and, when the provider adapter exposes them, the adapter-final transport payload (`provider_request_meta`, `provider_request_body`).
- `prompt_cache_key_hash` now means the caller-side cache family key for that turn, not the actual serialized request body.
- When a cache miss happens, compare `prompt_cache_key_hash` with `actual_request_hash` first:
  - same family key + different actual request usually means append-only growth, overlay differences, or tool-schema drift inside the same family;
  - different family key means the stable caller-side family boundary moved.
- `latest-context` now prefers that dedicated node actual-request artifact and only falls back to `messages_ref` for legacy nodes that predate the split.
- The node-detail `完整上下文` disclosure is intentionally on-demand. Frontend code should fetch `/latest-context` only when the operator explicitly expands that section; ordinary node-detail auto-refresh or patch reconciliation must not silently reload it in the background.
- The `/latest-context` payload still carries `ref`, `actual_request_ref`, and hash/count diagnostics for forensics, but its `content` field is now a human-facing rendering of the latest received context body rather than the entire artifact JSON. Backend formatting should prefer `request_messages`, then adapter-final `provider_request_body.input`, and only fall back to legacy projected `messages_ref` content when no dedicated actual-request body exists.
- For node troubleshooting, prefer `latest-context` or `actual_request_ref` when you need the latest provider-bound/request-facing context body, and treat the legacy projected input as a separate, compatibility-oriented lens.

CEO/frontdoor now follows a parallel debugging pattern, but with session-scoped files instead of task artifacts.

- Every CEO/frontdoor `call_model` round writes the full provider-facing request to `.g3ku/web-ceo-requests/<session>/...json`.
- The same request-artifact timeline now also includes internal frontdoor lanes such as `token_compression` and `inline_tool_reminder`, not only visible provider sends.
- Inflight / paused CEO snapshots expose only the latest `actual_request_path`, `prompt_cache_key_hash`, `actual_request_hash`, `actual_request_message_count`, `actual_tool_schema_hash`, and a short `actual_request_history`.
- Reopened completed sessions also have a compact recovery sidecar at `.g3ku/web-ceo-continuity/<session>.json`. That file is the authority for “what baseline/stage/compression state should be restored before the first new visible turn after restart,” but it is not a replacement for the full request JSON when doing request forensics.
- When debugging CEO prompt shrinkage or cache drops, inspect the saved request JSON first rather than inferring the request from `canonical_context`, stage rounds, or transcript-visible assistant/tool bubbles.
- The corresponding session-state projection now has two different meanings:
  - `dynamic_appendix_messages` keeps only the latest authoritative contract for rebuild purposes.
  - The active-turn `messages` / saved actual request may still include earlier same-turn contract snapshots so the provider-facing body can remain append-only.
- Those older contract snapshots are expected inside an active turn and should not be mistaken for durable-history leakage. After the turn closes, durable transcript messages must still be stripped back to non-contract history.
- For `/responses`-style providers, that saved JSON now includes both layers:
  - `request_messages` / `tool_schemas`: the runtime-side request projection before the provider adapter rewrites it
  - `provider_request_meta` / `provider_request_body`: the adapter-final HTTP body that was actually prepared for transport
- The saved JSON also carries `usage`, `frontdoor_history_shrink_reason`, and `frontdoor_token_preflight_diagnostics`, so the web/admin debugging flow can compare preflight estimates, allowed shrink reasons, and billed token usage from one record.
- If cache accounting disagrees with the runtime projection, debug against `provider_request_body` first.
- If a `/responses` gateway fails only when tools are present, inspect `provider_request_body.tools` before blaming the model route. The transport-facing schema bundle is intentionally sanitized to avoid unsupported combinators such as `anyOf`, `oneOf`, and `allOf`.
- For stage-transition rounds specifically, it is now expected that the tail runtime contract may show `callable_tool_names=["submit_next_stage"]` while `provider_request_body.tools` still carries the stable runtime-visible tool bundle. That mismatch is intentional and no longer indicates a frontdoor contract leak.
- For reopened completed sessions, the first fresh visible turn may bridge the previous `provider_tool_schema_names` and cache-family anchor only when the current `visible_tool_ids` plus `visible_skill_ids` exactly match the completed continuity snapshot. If the visible set changed, context should still restore but cache miss is allowed.

## Verification Pointers

Use these focused checks when validating i18n shell behavior:

- `python -m pytest tests/web/test_frontend_i18n.py -v`
- `python -m pytest tests/resources/test_bootstrap_runtime_status.py -v`

## CEO Compression UI Contract

The CEO composer now has a dedicated frontdoor-compression UI path that is separate from ordinary tool progress.

- `compression_state` only means inline frontdoor `token_compression` progress. The frontend should treat `status === "running"` as "the runtime is compressing context right now" and should not infer any durable semantic-summary state from it.
- While `compression_state.status === "running"`, the browser shows the `上下文压缩中` toast near the composer, but pause still goes through the existing primary send/pause button at the right side of the input row.
- The compression toast is intentionally left-aligned within the composer flow instead of floating over the right-side loader lane.
- When queued follow-up messages are present, the compression toast must render above that queued-message list rather than overlapping it.
- Clicking the ordinary primary `暂停` button during compression still sends the usual `client.pause_turn` request; the backend is responsible for cancelling compression and discarding any late compression result.
- When compression finishes, errors, is discarded by pause, or the turn ends, the compression toast must disappear.
- Tool-wait reminder labels from the reminder sidecar are live-only event data and should remain hidden from the visible CEO feed. They must not render as transcript lines, assistant bubbles, or persistent notices.

### Context Window Error UX

- If the estimated provider-bound request is already larger than the selected model's `context_window_tokens`, the frontend now shows an error toast instead of attempting a semantic/global-summary fallback.
- The canonical message is `上下文大小超出当前模型<展示名>，请更改模型链配置后继续`.
- `<展示名>` is expected to come from the runtime-selected model's `provider_model`, with model `key` only as fallback.

### Composer Context Usage Meter

- The Leader composer now has a second live-only context-size signal: a brain-shaped usage meter beside the attachment button, not a border around the textarea.
- That meter is backend-driven rather than a frontend-only guess, but it now has two distinct authority lanes that maintainers must keep separate.
- For idle/non-running sessions, the browser may debounce composer edits and call `POST /api/ceo/sessions/{session_id}/composer-preflight`.
- That preflight request payload should represent the next outbound user batch for that session: existing queued follow-ups plus the current unsent draft/attachments, in FIFO order.
- The preflight response should include the current model-facing estimate and threshold fields, including `estimated_total_tokens`, `context_window_tokens`, `ratio`, `provider_model`, `trigger_tokens`, `would_trigger_token_compression`, and `would_exceed_context_window`.
- For running sessions, the browser must stop treating composer-preflight as authoritative. The only valid source is the current inflight turn snapshot from `snapshot.ceo` / `ceo.turn.patch`, specifically the latest `frontdoor_token_preflight_diagnostics.final_request_tokens`, `frontdoor_token_preflight_diagnostics.max_context_tokens`, and `frontdoor_token_preflight_diagnostics.provider_model`.
- This means the visible meter during a running turn is no longer "draft if sent now"; it is "the actual next provider-bound request the runtime is about to send."
- When the current inflight snapshot does not yet carry an exact runtime request estimate, the meter must stay visually empty. Frontend code must not show `pending`, must not reuse the previous composer estimate, and must not infer a replacement value from the draft textarea, pinned sent entries, or `inflight_turn.user_message`.
- The brain meter itself is live-only UI state. It must animate with the current ratio, clamp visual fill when the raw ratio exceeds `1.0`, and never create transcript messages, assistant bubbles, or persisted snapshot entries.
- If the meter appears inconsistent with real send-time compression/error behavior, first check whether the browser is in the idle preflight lane or the running snapshot lane, then debug the corresponding backend source. Treat any browser-only fallback estimate during a running turn as a contract bug.

## Tool Admin RBAC Contract

Tool management now has a strict persisted-RBAC contract for surfaced tool families.

- The backend `PUT /api/resources/tools/{tool_id}/policy` path treats each action's `allowed_roles` as the authoritative whitelist.
- Clearing all checkboxes for a surfaced action is valid and persists as `[]`, meaning deny-all.
- Reopening the same tool after save or after `/api/resources/reload` must show the same empty-role state instead of silently restoring `ceo` or `execution`.

The frontend responsibilities are now:

- reflect the backend-owned `allowed_roles` exactly,
- allow all surfaced action role toggles to be unchecked,
- avoid special-casing CEO for surfaced core tool families,
- and show a clear operator-visible hint when an action is currently disabled for all roles.

The backend responsibilities are now:

- preserve explicit empty role lists through store readback and resource refresh,
- derive runtime visibility for surfaced tools from that persisted RBAC state,
- and keep internal non-Tool-Admin tools outside the Tool Admin contract.

Web/channel delivery maintenance note:

- Tool Admin no longer surfaces a `messaging` tool family, and the web runtime no longer applies any `G3KU_WEB_DISABLE_MESSAGE_TOOL` compatibility rule.
- Pure browser CEO chat replies are delivered through the websocket session/runtime path, not through a surfaced `message` tool.
- China-channel replies continue to flow through `SessionRuntimeBridge` and `ChinaBridgeTransport`, which emit final `deliver_message` frames without depending on the removed model tool.
- If an operator reports that `messaging` still appears in Tool Admin, debug resource discovery or stale frontend state first; the intended contract is absence, not "present but disabled".

Exec runtime mode is now part of the same admin contract for `exec_runtime`:

- `PUT /api/resources/tools/exec_runtime/policy` may carry `execution_mode` in addition to action-role edits.
- Tool Admin is responsible for exposing and saving the operator's chosen mode, but the backend remains authoritative for validation and persistence.
- Saving `execution_mode` must update the persisted surfaced family record immediately; operators should not need a project restart before later agent/tool calls observe the new mode.
- Resource reload may still be used for catalog refresh, but it is no longer the mechanism that makes exec mode changes take effect. If a mode switch appears to require restart, first inspect the policy save payload and the persisted `tool_families` row before debugging frontend caching.

If an operator reports "save succeeded but reopen restored the roles", first inspect:

1. the Tool Admin save payload,
2. the stored `tool_families.payload_json` row for that surfaced family,
3. the post-reload `GET /api/resources/tools/{tool_id}` response.

Do not start with frontdoor prompt debugging unless those three layers already agree.

## CEO Canonical Context UI Contract

The CEO browser/runtime integration now uses `canonical_context` as the only stage-trace protocol field.

- Assistant transcript messages, `snapshot.ceo`, `ceo.turn.patch`, preserved-turn payloads, paused snapshots, and `ceo.reply.final` should carry a turn-scoped `canonical_context`.
- The frontend should not read or reconstruct CEO stage flow from `execution_trace_summary` or flat `tool_events`.
- `canonical_context.stages[].rounds[].tools[]` is the authoritative render source for the stage trace.
- The frontend should treat live/current-turn `canonical_context` as bubble-local trace data, not as the session's full durable stage history.

Tool output rendering should follow the canonical payload directly:

- if a tool entry has `output_text`, the browser should show that inline full text;
- if a tool entry only has `output_ref`, the browser should show the preview text and keep the existing artifact-open path for the full body;
- the frontend should not invent extra truncation or backfill old tool-event text when canonical context is present.

The final-reply rule is now simpler:

- `ceo.reply.final` may include `canonical_context` when the completed turn has stage data;
- if the turn has no stage trace, omit `canonical_context` entirely rather than reusing an older turn's trace.

## Heartbeat/Cron ACK Contract

The browser now handles a dedicated live-only ACK event for silent internal turns.

- `ceo.internal.ack` is emitted when a heartbeat or cron turn explicitly ends with `HEARTBEAT_OK`.
- This event is not a normal assistant reply and must not reuse `ceo.reply.final` persistence or rendering rules.
- Any non-silent heartbeat/cron assistant reply now uses the ordinary `message_end -> ceo.reply.final` path. The backend should no longer blanket-filter heartbeat assistant finals from websocket delivery just because the source is internal.
- The frontend should render it as a distinct non-conversational bubble so operators can see that the internal turn was received and intentionally stayed silent.
- That ACK bubble is intentionally ephemeral: it should not be appended to the CEO session snapshot `messages` list and should disappear on full refresh.
- `ceo.turn.discard` still exists only to close a specific visible pending turn by `turn_id`.
- `task_terminal` is now an explicit exception: heartbeat task-terminal turns should no longer reach the browser as `ceo.internal.ack`.
- If the heartbeat model first produces `HEARTBEAT_OK` or empty text for a task terminal event, the backend now enters repair rounds and only sends `ceo.reply.final` once there is a user-visible result or the fixed fallback error is emitted.

## CEO Live Tool Reminder Contract

The CEO browser/runtime integration now has a second live-only status lane for long-running direct tools: `ceo.tool.reminder`.

This is intentionally different from both ordinary tool interaction steps and heartbeat turns.

- Backend reminder events are emitted only as websocket live events.
- The payload includes `turn_id`, `execution_id`, `tool_name`, `elapsed_seconds`, `reminder_count`, `decision`, `label`, `source="reminder"`, and optional `terminal`.
- The frontend must not create a new assistant bubble and must not append a new interaction step for reminder events.
- The current CEO frontend also no longer renders `label` as a visible reminder block under the pending turn. These events are kept as live-only bookkeeping signals so the UI stays clean while the authoritative tool outcome still arrives through the ordinary tool/error/final-reply path.

### Persistence Rules

- Reminder events are not part of `snapshot.ceo.messages`.
- They must not be persisted into the transcript-backed CEO message list.
- Refresh/reconnect should not restore an old reminder from cached snapshot state.
- Any ephemeral reminder state should be cleared when the tool finishes, the turn finalizes, the turn is discarded, or a `terminal=true` reminder event arrives.

### Decision Semantics

- `decision=continue` means the sidecar reviewed the context and decided to keep waiting.
- `decision=stop` means the sidecar requested `stop_tool_execution`; the main turn will later surface the actual tool failure through the ordinary tool-result path.
- `decision=unavailable` means the reminder sidecar failed or could not make a valid stop decision, so the tool keeps running.

Operators should therefore treat `ceo.tool.reminder` as a live runtime signal, not as durable conversation UI. The authoritative end state still arrives through the normal CEO tool/error/final-reply events.

## Node Actual Request Forensics

Node actual-request artifacts now expose a clearer split between runtime contract and provider cache scaffolding.

- `callable_tool_names` records the real tool contract for that round.
- `provider_tool_names` records the schema bundle actually sent to the model provider.
- `provider_tool_bundle_seeded=true` means the node temporarily reused the prior provider bundle to avoid avoidable cache-family churn during a schema warm-up step.

For cache troubleshooting, the most important invariant is now:

- stage/tool governance still follows `callable_tool_names`;
- cache behavior must be explained using `provider_request_body.input`, `provider_tool_names`, and `actual_tool_schema_hash`.

If a node shows low cache hits even after `actual_tool_schema_hash` stops changing, operators should immediately compare consecutive node actual-request artifacts and verify that the provider input stayed append-only rather than replacing an earlier `function_call` / `function_call_output` pair near the front of the request.
