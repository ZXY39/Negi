# G3KU Heartbeat System

This document describes the maintenance boundary around the Web CEO heartbeat path and the CEO inline tool reminder sidecar.

## Responsibilities

- Heartbeat is the session-owned internal turn mechanism for work that must wake an existing CEO session without a new visible user message; typical inputs are detached/background task lifecycle changes, stall notices, and other session-owned follow-up events.
- Heartbeat runs through `RuntimeAgentSession.prompt(...)` as an internal turn with its own source metadata and rules.

## What Heartbeat Owns

- Internal wake/enqueue/scheduling behavior for existing CEO sessions.
- Session-visible internal turns that may end in `ceo.internal.ack`, `ceo.reply.final`, or `ceo.turn.discard`.
- The repair/fallback path for task-terminal cases that are not allowed to stay silent.
- The maintenance boundary between live UI state and durable transcript history for internal turns.

## Continuation Contract

- Heartbeat and cron are assembled through the session-owned `frontdoor_request_body_messages` / actual-request scaffold used by the next visible CEO turn, not a separate short `ceo_heartbeat` request lane.
- Each internal activation resumes from that same scaffold.
- When the authoritative frontdoor baseline already carries prior contract state, heartbeat and cron inherit the previous callable/candidate/hydrated/provider-tool/visible-skill state instead of rerunning tool or skill selection.
- If no authoritative frontdoor baseline exists yet, internal turns fall back to the ordinary CEO/frontdoor exposure assembly path for that round.
- Recovery of that baseline follows one strict restore order before a heartbeat turn runs: paused snapshot, then inflight snapshot, then completed continuity sidecar, then latest actual-request artifact. A stale sidecar lacking `frontdoor_request_body_messages` does not block recovery from a richer later source.
- Heartbeat still appends two hidden durable messages before the model call: a `system` rule message and a `user` event-bundle message. The rule text lives only in the `system` message; the `user` message carries the `[SESSION EVENTS]` bundle alone, so the rules text is never duplicated into the user turn.
- Cron appends two hidden durable `system` messages:
  - a cron rule message
  - a structured cron event block
- Those internal prompt messages must be persisted with `prompt_visible=true`, `ui_visible=false`, and an `internal_prompt_kind` that distinguishes heartbeat vs cron rule/event records.
- Because the request is append-only against the previous authoritative scaffold, heartbeat/cron share the same prompt-cache family, token-preflight, token-compression, and continuity rules as ordinary CEO/frontdoor turns.
- Silent `HEARTBEAT_OK` is the only live-only exception. An internal turn's real assistant reply is durable transcript history and, whenever non-empty and not `HEARTBEAT_OK`, finalize folds it into the request-body baseline (keyed on output content, never on turn kind). Baseline forensics: `context-and-cache-troubleshooting.md`「finalize 没把 direct reply 补回 baseline」.

## Persistence And UI Boundary

- Hidden heartbeat/cron rule + event-bundle messages are durable prompt history. They should participate in later prompt assembly, request artifacts, completed continuity sidecars, and compression just like any other prompt-visible message.
- Transcript views, session preview text, message counts, and `snapshot.ceo.messages` hide internal prompt messages by filtering `ui_visible=false` — never by assuming internal turns are transcript-hidden.
- Heartbeat/cron assistant replies, tool calls, tool results, and stage/compression traces remain ordinary visible turn output unless the turn ends with the silent `HEARTBEAT_OK` ACK path.
- Manual pause during a running heartbeat/cron turn goes through the ordinary `client.pause_turn` path; the backend treats that internal turn as the current active turn, not a side lane.
- Maintenance boundary: an internal heartbeat/cron request artifact may be authoritative for billing and forensics without replacing the session-owned baseline. If the new durable body is mostly heartbeat rule/event text and poorer than the existing baseline without a `token_compression` / `stage_compaction` explanation, the runtime keeps the richer baseline instead of promoting the internal-only body into completed continuity.

## Cron Reminder Contract

- Cron is a structured reminder mechanism for the future agent, not a natural-language stop-condition engine; a cron `message` is the reminder instruction for that future agent, not a ready-to-send user reply.
- Cron-internal and heartbeat-internal turns are not an internal-only tool lane: they reuse the ordinary CEO/frontdoor tool exposure for the current role (inheriting prior contract state when present), and their callable/visible set always includes `submit_next_stage`, so a turn without an active stage opens one first and proceeds; the execution gate and the exposed contract cannot disagree on internal turns.
- Node-pause (`task_node_error`) heartbeat turns are the one exception to "open a stage first": they run stageless so the first `manage_task_nodes`/`task_node_detail` call does not die on a guaranteed `no active stage` error. When that first substantive tool lands without an active stage, `_frontdoor_stage_state_after_tool_cycle` auto-opens a `system_generated` stage (budget 10, title of the form "任务 ID xxx 中的节点出现自动暂停，检查原因并处理") and books the tool into it; once that budget is exhausted the normal gate applies and the turn must `submit_next_stage`. Trigger context for the auto-opened stage travels via `heartbeat_reason` / `heartbeat_task_ids` / `heartbeat_node_ids` in the turn metadata.
- The prompt-side cron rule is intentionally minimal: the reminder is an internal instruction, not a new user message, to be executed immediately.
- The runtime does not hard-code a prompt-side ban on cron-tool mutations during reminder turns: if the reminded work itself is a currently visible CEO action (e.g. sending a reminder, creating another cron), the model may use the ordinary CEO tool surface.
- Repetition is enforced by service-side counters (`payload.max_runs` / `state.delivered_runs`): a reminder only counts as delivered after the internal prompt is durably accepted by the runtime/session path; when `delivered_runs >= max_runs`, the service removes the job and schedules no further wakeup.
- One-shot `at` reminders are validated against the service clock at creation: a target timestamp already in the past when `add_job()` runs is rejected immediately (the error quotes service-local time and directs immediate execution or abandonment) instead of storing a dormant expired job.
- One-shot `at` creation is guarded against duplicate registration: `add_job()` rejects a new `at` job when an enabled job already exists for the same `(session_key, at_ms)` pair, returning `同一会话在 <time> 已存在一次性提醒 (id: …)，请勿重复创建；如需修改请先用 remove 删除旧任务，或改用其他时间`. The match is structural, not text-based, so reworded re-adds within one turn are caught. Disabled jobs never block a fresh registration; recurring schedules are exempt.
- If an old cron store uses the previous schema version, the runtime drops those jobs instead of attempting migration; maintainers should treat this as an intentional semantic reset.

Cron job delivery is claim-before-dispatch, which defines the restart/recovery guarantee:

- Before invoking a job handler, the cron service persists a run claim to `.g3ku/cron/jobs.json` (`state.last_run_at_ms` set, `state.last_status = "running"`). Because `_recompute_next_runs()` never re-arms an `at` job whose `last_run_at_ms` is already set, a one-shot reminder is **at-most-once across restarts**: a crash mid-dispatch cannot re-trigger it.
- Store writes are atomic (temp-file + replace). A crash mid-write cannot truncate `jobs.json`; a truncated store would be read back as corrupt and reset, which would silently drop claims and re-arm already-dispatched jobs.
- On startup, any job still marked `running` is a run claimed but never finalized (restart happened between claim and finalize). The service reconciles these into `last_status = "interrupted"`: one-shot `at` jobs are suppressed — disabled, not re-dispatched — because a duplicate reminder is worse than a missed one and the original dispatch may already have reached the downstream handler; recurring jobs simply resume their schedule.
- An in-flight guard per job id also prevents an overlapping timer tick and a manual `run_job` for the same job from producing a second concurrent dispatch.
- New-maintainer caveat: a one-shot reminder that never fires after a restart usually has an `interrupted` / `running` store state — that is the *expected* at-most-once outcome of a crash between claim and finalize, not a scheduler bug, and must not be "fixed" by re-arming a duplicate job.

## Task Terminal Repair Contract

- Task-terminal heartbeat only repairs or produces the session reply for an existing terminal event; it never auto-runs `continue_task`, creates replacement tasks, or retries failed tasks in place.
- If a task still needs more work after terminalization, that must come from a later explicit frontdoor/user decision, typically via `create_async_task`.
- Task-terminal callback persistence and heartbeat queueing have separate duplicate boundaries: the outbox row is the durable callback boundary, and the heartbeat event queue only dedupes currently enqueued in-memory events.
- Both boundaries key on the canonical dedupe key `task-terminal:{task_id}:{status}:{finished_at}`. `normalize_task_terminal_payload` always recomputes this key server-side and ignores any caller-supplied `dedupe_key`/`dedupeKey`; probe/retry key variants cannot bypass exact-key dedupe. Do not reintroduce caller-controlled dedupe keys.
- Because in-memory dedupe is transient, `/api/internal/task-terminal` also rejects a repeated callback when the same outbox row is already `accepted=true` (even if `delivery_state` is not yet `delivered`). Debugging "same failed task spawned two heartbeat replies": inspect the outbox row before blaming prompt behavior.
- Session-level reply dedupe: heartbeat persists the `dedupe_key` of every `task_terminal` event that produced a visible reply into `session.metadata["handled_terminal_dedupe_keys"]` (bounded, string-only, de-duplicated). A later delivery of the same key is consumed silently (`pop_many` + `mark_task_terminal_outbox_delivered`) without running the agent or re-publishing `ceo.reply.final` — guarding the crash window between reply persist and queue-pop/outbox-ack.
- The heartbeat final event (`ceo.reply.final`) carries the same canonical-context fields as the user-lane relay: `canonical_context` + `canonical_context_delta` appear only when the just-persisted reply added new stage progress; an empty delta omits both so the browser renders a plain reply bubble and never re-submits accumulated stage rails.
- The task-terminal event payload has two result lanes: `terminal_*` describes the true terminal node (when final acceptance fails this stays the acceptance-node result), and `root_output` / `root_output_ref` carries the root execution deliverable so the full root-node output reaches the main agent even when the terminal node is `acceptance`.
- Heartbeat task-terminal prompt assembly therefore renders both pieces when final acceptance fails: the acceptance-node result (`Result output`, `Result check`, `Result failure reason`) and the root execution deliverable (`Execution output`, `Execution output ref`).
- The full root execution output requirement applies to the heartbeat event bundle that the main agent reads, not to every later summary surface.
- Externalized terminal output that fits the content-open inline budget is re-inlined: enrichment resolves `terminal_output` / `root_output` through their `*_output_ref` and, when the referenced text is textual and within 16000 chars / 260 lines, replaces the summary with the full text so the turn can deliver the complete result without a follow-up `content_open`; oversized or non-textual output keeps summary + ref.
- China-channel reply delivery: for a `china:*` session, the persisted reply is also handed to `reply_notifier` → `_notify_heartbeat_channel_reply`, which publishes an `OutboundMessage` for the China drain. The channel/chat_id must come from china session-key parsing, never a naive first-colon split, and must not overwrite the owning transport's authoritative session meta — otherwise the outbound carries a wrong `channel` and is dropped. See `china-channels.md` §7.
- Heartbeat service instance reuse: `build_web_session_heartbeat` reuses the live instance bound to the same agent/runtime-manager/task-service/session-manager. The reply-notifier closure cannot be compared by identity (callers pass a fresh one); an identity-based reuse check rebuilds a not-started instance, orphaning the started one and stranding enqueued events on it.

## Task Node Error Delivery

- It scans at startup and every 60 seconds for undelivered `pause_reason=error` rows whose nodes remain `in_progress` and `is_paused`.
- It groups rows by source session and enqueues `task_node_error` items with task/node identity, error text, reason, remark, and dedupe key `node-error:{task_id}:{node_id}:{pause_row_id}`.
- It marks a row `delivered` only after enqueue accepts the payload. Failed enqueue retries on the next scan; delivered rows are not resent after restart.
- `prompt_lane` renders the event as a decision to use `manage_task_nodes` for resume, keep-paused-with-remark, fail, or related-node pause. Tool validation belongs to the tool contract; delivery belongs here.
- A heartbeat turn that fails before finishing does not pop its events, so the wake queue re-delivers them. For `task_node_error` events failures are bounded per session: after 3 consecutive failed turns the events are dequeued and a visible give-up reply is persisted + notified (the node stays paused for the user or a later turn); any successful turn resets the streak. Debugging "node error heartbeats retry forever": the scanner enqueues each pause row only once — suspect queue/streak interaction.

## What Heartbeat Does Not Own

The CEO inline tool reminder lane is not a heartbeat turn.

- It enqueues no heartbeat event, calls neither `RuntimeAgentSession.prompt(...)` nor the normal turn lock / heartbeat running gate, and writes no transcript history, canonical context, or persistent session state.

This distinction matters when debugging long-running CEO direct tools: a reminder event is not evidence that a new internal turn happened.

## CEO Inline Tool Reminder Sidecar

CEO frontdoor direct long-running tools use a live-only sidecar reminder lane.

- Inline executions register in `InlineToolExecutionRegistry` (not the detached `ToolExecutionManager` background-execution path); reminder windows are fixed at `30 / 60 / 120 / 240 / 600` seconds, repeating every 600 seconds after the 600-second window.
- Argument-owned timeout opt-out: if normalized top-level tool arguments already contain a timeout-bearing key (e.g. `timeout_seconds`), the runtime skips reminder-sidecar stop/continue decisions and leaves timeout ownership to the tool.
- With an authoritative CEO actual-request JSON, `CeoToolReminderService` reuses that saved `request_messages` / `tool_schemas` / `prompt_cache_key` / `parallel_tool_calls` scaffold as cache prefix and appends only live reminder-tail messages; otherwise it falls back to a read-only `CeoMessageBuilder.build_for_ceo(..., ephemeral_tail_messages=...)` rebuild.
- The sidecar reuses the CEO main model binding, but its decision channel is text-only (`STOP` / `CONTINUE`); even when it reuses the main turn's full provider-visible tool bundle for cache-prefix stability, it must not execute arbitrary returned tool calls.
- The reminder decision is observation-aware before the model is consulted: the sidecar reads the current tool name, normalized arguments, and the latest live `sidecar_observation` payload when available. `sidecar_observation` is a generic progress-side channel; `agent_browser` is the first producer. See `web-and-admin.md`「CEO Live Tool Reminder Contract」 for UI rendering of reminder events.
- Reminder labels remain live-only event data and must not be persisted into transcript, canonical context, or history injection.
- Reminder snapshots may carry the visible stage view, compression progress, hydrated tools, selection debug, and actual-request pointer for cache forensics; no semantic/global-summary persistence lane is associated with reminder handling.

## Reminder Failure Semantics

- If the reminder model call fails, times out, or returns unusable output, the default decision is `unavailable`; `unavailable` reminders do not stop the tool and do not interrupt the main turn.
- Only an explicit sidecar stop decision is allowed to stop the inline execution.
- If the latest observation shows a concrete hard tool failure, the sidecar keeps waiting so the tool can complete and surface its native error instead of converting that state into a timeout stop; the same applies for positive progress (current URL, page title, or an explicit progress marker).

## Timeout Stop Contract

When the sidecar stops a running CEO direct tool, the stop flows back into the main turn as an ordinary tool failure, not a silent cancel.

- The registry stores `InlineToolStopDecisionMetadata` with `reason_code=sidecar_timeout_stop`, which the direct tool completion path normalizes into a `tool_error` / `status=error` result visible to the main turn.
- The error text must include the tool name, elapsed runtime, reminder count, and the fact that the stop came from a sidecar timeout decision — e.g., `Error executing exec: stopped by sidecar timeout decision after 120.4s (2 reminders).`
- Sidecar timeout-stop targets only the inline execution's child cancellation token; it never marks the whole visible turn as cancelled or causes later tool calls in that same turn to fail immediately.
- If the tool finishes successfully before the stop lands, the runtime clears the timeout-stop metadata and preserves the successful result.

## Web/UI Boundary

- The sidecar publishes `ceo.tool.reminder` live websocket events; browsers may keep them entirely non-visual (the current CEO frontend does not render the reminder label as a visible text block under the pending turn).
- They do not become `snapshot.ceo` messages and are not restored on refresh or reconnect.

When debugging a “reminder appeared but the transcript stayed clean” report, that is the intended contract.
