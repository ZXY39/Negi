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
- Sending while a turn is active enqueues a follow-up instead of interrupting the current turn.
- Once the session becomes dispatchable again, queued follow-ups are drained in FIFO order into one fresh outbound batch.
- Each queued item still remains its own user message in the frontend timeline and in transcript persistence; batching only changes how the next LLM call is assembled.

### 3. Context Loader Notices

- Successful CEO/frontdoor `load_tool_context` and `load_skill_context` calls are no longer shown as ordinary `Interaction Flow` steps under the assistant bubble.
- Instead, the browser shows a short-lived composer notice above the input row, using the loaded `tool_id` or `skill_id` when the runtime payload exposes it.
- These notices are intentionally stackable rather than single-slot: multiple successful loader calls may coexist in a small floating notice stack above the composer.
- The intended motion contract is "launch from the composer, settle into the notice stack, then fade out"; the full lifecycle is currently about 5 seconds per notice.
- That notice is intentionally live-only UI state. It should fade away after a short timeout and must not be appended into the persisted CEO session `messages` list.

### Manual Pause Resume Rule

- Manual pause now freezes the current turn as the previous round context instead of waiting for a textual resume merge.
- The next outbound user message after pause must start a new round.
- The paused round's user message, execution trace, stage state, tool calls, and compression state are preserved in transcript and snapshot context so the next round can inherit them without rewriting the original user text.
- Before that new user turn is dispatched, the backend now archives the previous paused assistant bubble into a persisted assistant message with `status=paused`.
- That archived paused assistant is durable UI history for `snapshot.ceo` restore/reconnect, but it remains hidden from prompt-history assembly and session-summary counts via `history_visible=false`.
- Browser-side restore should therefore render that persisted paused assistant as a paused bubble rather than a completed reply, while the next turn's actual prompt inheritance still comes from paused execution context and other visible history sources.

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
- Once heartbeat finishes and the session becomes dispatchable again, queued follow-ups may begin draining automatically.

### Task Hall Action Contract

- The browser task hall now only exposes `pause`, `resume`, and `delete` task actions.
- `retry`, `continue-evaluate`, and `open continuation` actions are removed from both the UI flow and the REST surface.
- Task list and task detail status pills now derive from the current task `status` plus final-acceptance state; legacy continuation metadata fields are ignored even if older task records still carry them.

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
- Backend clear handling for channel sessions must remove the persisted `SessionManager` transcript for that `china:*` session key, invalidate any in-memory cached session object, and clear the same side artifacts that local-session deletion clears for that session id, including inflight snapshots, paused execution context, uploads, and frontdoor stage-archive artifacts.
- For DM channel rows, the catalog entry may still remain visible after clear because it is synthesized from enabled channel-account configuration rather than from transcript persistence alone.

If an operator reports “the channel conversation was deleted but old context came back,” inspect these layers in order:

1. `DELETE /api/ceo/sessions/{session_id}` response payload for `cleared=true`
2. persisted `sessions/china_*.jsonl` transcript files and in-memory `SessionManager` cache
3. inflight / paused CEO session artifacts
4. frontend snapshot cache only after the backend-owned state is confirmed cleared

### Heartbeat/Cron Visibility Versus Prompt Inheritance

- Browser-side CEO timeline rendering and inflight bubbles are allowed to show heartbeat / cron 的原始处理流程。
- This is sourced from session/inflight snapshots, not from the next real user turn's near-field prompt history.
- Maintainers should not assume "frontend can see it" means "the next prompt inherits it verbatim".

The current rule is:

- UI may show heartbeat / cron stage openings, tool calls, execution trace, and compression state directly.
- The next real user turn still filters internal-only history from its local raw history injection.
- To avoid forgetting that work, heartbeat / cron agent-side raw execution context is expected to flow into the global semantic summary path, so later user turns can recover the important meaning without replaying the entire internal turn transcript.

## Actual Request Debugging Contract

Node detail and latest-context views now expose two different debugging surfaces that maintainers must not mix up.

- The existing node input / projected context view is still a projection-oriented operator surface. It is useful for understanding durable state and task intent, but it is not guaranteed to be the exact request body sent to the provider.
- The actual provider request is represented separately through `actual_request_ref`, `actual_request_hash`, `actual_request_message_count`, and `actual_tool_schema_hash`.
- `prompt_cache_key_hash` now means the caller-side cache family key for that turn, not the actual serialized request body.
- When a cache miss happens, compare `prompt_cache_key_hash` with `actual_request_hash` first:
  - same family key + different actual request usually means append-only growth, overlay differences, or tool-schema drift inside the same family;
  - different family key means the stable caller-side family boundary moved.
- For node troubleshooting, prefer `latest-context` or `actual_request_ref` when you need the exact provider-facing request, and treat the legacy projected input as a separate, compatibility-oriented lens.

CEO/frontdoor now follows a parallel debugging pattern, but with session-scoped files instead of task artifacts.

- Every CEO/frontdoor `call_model` round writes the full provider-facing request to `.g3ku/web-ceo-requests/<session>/...json`.
- Inflight / paused CEO snapshots expose only the latest `actual_request_path`, `prompt_cache_key_hash`, `actual_request_hash`, `actual_request_message_count`, `actual_tool_schema_hash`, and a short `actual_request_history`.
- When debugging CEO prompt shrinkage or cache drops, inspect the saved request JSON first rather than inferring the request from `canonical_context`, stage rounds, or transcript-visible assistant/tool bubbles.

## Verification Pointers

Use these focused checks when validating i18n shell behavior:

- `python -m pytest tests/web/test_frontend_i18n.py -v`
- `python -m pytest tests/resources/test_bootstrap_runtime_status.py -v`

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

Web-only maintenance note:

- When `G3KU_TASK_RUNTIME_ROLE=web`, the runtime auto-disables the `messaging` tool family (executor: `message`) on startup by default to prevent accidental external-channel sends in the pure browser chat UI.
- This default is controlled by `G3KU_WEB_DISABLE_MESSAGE_TOOL` (unset means enabled; set to `0/false/no/off` to keep `messaging` enabled).
- Tool Admin surfaces this as the `messaging` tool family being disabled in the catalog.

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

## CEO Compression UI Contract

The browser shell still receives `compression` snapshot data, but it now refers only to semantic-summary refresh state.

- The UI should not assume there is any older message-count compaction stage behind this field.
- `compression.status` now reflects semantic-summary lifecycle only.
- The frontend may still display heartbeat / cron live execution and compression activity from snapshots, but later real user turns depend on the semantic summary path rather than any hidden legacy history compactor.

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
- The frontend renders the reminder inside the active pending CEO turn, below the `Interaction Flow`, using a dedicated reminder block.
- The frontend must update the existing reminder in place for the same `execution_id`; it must not create a new assistant bubble and must not append a new interaction step.

### Persistence Rules

- Reminder events are not part of `snapshot.ceo.messages`.
- They must not be persisted into the transcript-backed CEO message list.
- Refresh/reconnect should not restore an old reminder from cached snapshot state.
- The reminder block should be cleared when the tool finishes, the turn finalizes, the turn is discarded, or a `terminal=true` reminder event arrives.

### Decision Semantics

- `decision=continue` means the sidecar reviewed the context and decided to keep waiting.
- `decision=stop` means the sidecar requested `stop_tool_execution`; the main turn will later surface the actual tool failure through the ordinary tool-result path.
- `decision=unavailable` means the reminder sidecar failed or could not make a valid stop decision, so the tool keeps running.

Operators should therefore read the reminder block as live guidance only. The authoritative end state still arrives through the normal CEO tool/error/final-reply events.
