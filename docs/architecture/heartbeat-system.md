# G3KU Heartbeat System

This document describes the maintenance boundary around the Web CEO heartbeat path and the CEO inline tool reminder sidecar.

## Responsibilities

- Heartbeat is the session-owned internal turn mechanism for work that must wake an existing CEO session without a new visible user message.
- Typical heartbeat inputs are detached/background task lifecycle changes, stall notices, and other session-owned follow-up events.
- Heartbeat still runs through `RuntimeAgentSession.prompt(...)` as an internal turn with its own source metadata and rules.

## What Heartbeat Owns

- Internal wake/enqueue/scheduling behavior for existing CEO sessions.
- Session-visible internal turns that may end in `ceo.internal.ack`, `ceo.reply.final`, or `ceo.turn.discard`.
- The repair/fallback path for task-terminal cases that are not allowed to stay silent.
- The maintenance boundary between live UI state and durable transcript history for internal turns.

## Continuation Contract

- Heartbeat and cron are assembled through the session-owned `frontdoor_request_body_messages` / actual-request scaffold used by the next visible CEO turn, not through a separate short `ceo_heartbeat` request lane on the main CEO path.
- Each internal activation resumes from that same scaffold.
- When that authoritative frontdoor baseline already carries prior frontdoor contract state, heartbeat and cron inherit the previous callable/candidate/hydrated/provider-tool/visible-skill state directly instead of rerunning tool or skill selection.
- If no authoritative frontdoor baseline exists yet, internal turns fall back to the ordinary CEO/frontdoor exposure assembly path for that round.
- Recovery of that baseline follows one strict restore order before a heartbeat turn runs: paused snapshot, then inflight snapshot, then completed continuity sidecar, then latest actual-request artifact. A stale sidecar file that lacks `frontdoor_request_body_messages` is not enough to block recovery from a richer later source.
- Heartbeat still appends two hidden durable messages before the model call:
  - a `system` rule message
  - a `user` event-bundle message
- Cron appends two hidden durable `system` messages:
  - a cron rule message
  - a structured cron event block
- Those internal prompt messages must be persisted with `prompt_visible=true`, `ui_visible=false`, and an `internal_prompt_kind` that distinguishes heartbeat vs cron rule/event records.
- Because the request is append-only against the previous authoritative scaffold, heartbeat/cron share the same prompt-cache family, token-preflight, token-compression, and continuity rules as ordinary CEO/frontdoor turns.
- Silent `HEARTBEAT_OK` remains the only live-only exception. If an internal turn produces a real assistant reply, that reply is durable transcript history and should remain visible to later prompt assembly.

## Persistence And UI Boundary

- Hidden heartbeat/cron rule + event-bundle messages are durable prompt history. They should participate in later prompt assembly, request artifacts, completed continuity sidecars, and compression just like any other prompt-visible message.
- Frontend transcript views, session preview text, session message counts, and `snapshot.ceo.messages` must hide those internal prompt messages by filtering `ui_visible=false`, not by assuming every internal turn is transcript-hidden.
- Heartbeat/cron assistant replies, tool calls, tool results, and stage/compression traces remain ordinary visible turn output unless the turn ends with the silent `HEARTBEAT_OK` ACK path.
- Manual pause during a running heartbeat/cron turn still goes through the ordinary `client.pause_turn` path. The backend should treat that internal turn as the current active turn rather than as a side lane.
- Maintenance boundary: an internal heartbeat/cron request artifact may be authoritative for billing and request forensics without being allowed to replace the session-owned baseline. If the new durable body is mostly heartbeat rule/event text, shorter/poorer than the existing baseline, and not explained by `token_compression` or `stage_compaction`, runtime must keep the richer baseline for the next turn instead of promoting the internal-only body into completed continuity.

## Cron Reminder Contract

- Cron is a structured reminder mechanism for the future agent, not a natural-language stop-condition engine.
- Cron `message` should be understood as the reminder instruction for the future agent, not as a ready-to-send user reply.
- Cron-internal turns are not a cron-only tool lane. When prior frontdoor contract state exists, they reuse the ordinary CEO/frontdoor tool exposure for the current role; the special-case is only that they bypass the normal “no valid stage => `submit_next_stage` only” shrink so a scheduled reminder can immediately call `create_async_task`, task query builtins, or other already-visible CEO tools.
- The prompt-side cron rule is intentionally minimal: it tells the model that the reminder is an internal instruction, not a new user message, and that it should execute the reminded work immediately.
- The runtime does not hard-code a prompt-side ban on cron-tool mutations during structured reminder turns. If the reminded work itself is “send a plain-text reminder”, “create another cron”, or any other currently visible CEO action, the model may use the ordinary CEO tool surface for that work.
- Repetition is enforced by service-side counters:
  - `payload.max_runs`
  - `state.delivered_runs`
- A cron reminder only counts as delivered after the internal prompt is durably accepted by the runtime/session path.
- When `delivered_runs >= max_runs`, the cron service removes the job immediately and does not schedule another wakeup.
- One-shot `at` reminders are validated at creation time against the service clock. If the target timestamp is already in the past when `add_job()` runs, the cron service rejects creation immediately (the error quotes the service-local time and directs immediate execution or abandonment) instead of storing a dormant expired job.
- One-shot `at` creation is also guarded against duplicate registration. `add_job()` rejects a new `at` job when an enabled job already exists for the same `(session_key, at_ms)` pair, returning `同一会话在 <time> 已存在一次性提醒 (id: …)，请勿重复创建；如需修改请先用 remove 删除旧任务，或改用其他时间`. The match is structural, not text-based: message wording is ignored, so reworded re-adds of the same reminder within one turn are caught. Different sessions may share a fire time; disabled jobs never block a fresh registration; recurring schedules are exempt. This complements claim-before-dispatch: that guarantee stops one job firing twice across a restart, this one stops two jobs existing for the same reminder.
- If an old cron store uses the previous schema version, the runtime drops those jobs instead of attempting migration; maintainers should treat this as an intentional semantic reset.

Cron job delivery is claim-before-dispatch, which defines the restart/recovery guarantee:

- Before invoking a job handler, the cron service persists a run claim to `.g3ku/cron/jobs.json` (`state.last_run_at_ms` set, `state.last_status = "running"`). Because `_recompute_next_runs()` never re-arms an `at` job whose `last_run_at_ms` is already set, a one-shot reminder is **at-most-once across restarts**: a crash mid-dispatch cannot re-trigger it.
- Store writes are atomic (temp-file + replace). A crash mid-write cannot truncate `jobs.json`; a truncated store would be read back as corrupt and reset, which would silently drop claims and re-arm already-dispatched jobs.
- On startup, any job still marked `running` is a run that was claimed but never finalized (the process restarted between claim and finalize). The service reconciles these into `last_status = "interrupted"`:
  - one-shot `at` jobs are suppressed — disabled and not re-dispatched — because a duplicate reminder is worse than a missed one, and the original dispatch may already have reached the downstream handler;
  - recurring jobs simply resume their schedule.
- The service also keeps an in-flight guard per job id, so an overlapping timer tick and a manual `run_job` for the same job do not produce a second concurrent dispatch.
- New-maintainer caveat: if a one-shot reminder appears to have "not fired" after a restart, check for an `interrupted` / `running` state in the store first. The at-most-once guarantee means a missed one-shot is the *expected* outcome of a crash between claim and finalize, not a scheduler bug, and it must not be "fixed" by re-arming a duplicate job.

## Task Terminal Repair Contract

- Task-terminal heartbeat only repairs or produces the session reply for an existing terminal event.
- It does not auto-run `continue_task`, create replacement tasks, or retry failed tasks in place.
- If a task still needs more work after terminalization, that must come from a later explicit frontdoor/user decision, typically via `create_async_task`.
- Task-terminal callback persistence and heartbeat queueing have separate duplicate boundaries:
  - the outbox row is the durable callback boundary,
  - the heartbeat event queue only dedupes currently enqueued in-memory events.
- Because in-memory dedupe is transient, `/api/internal/task-terminal` must also reject a repeated callback when the same outbox row is already `accepted=true` even if it is not yet `delivery_state=delivered`. Maintainers debugging "same failed task spawned two heartbeat replies" should inspect the task-terminal outbox row before blaming prompt behavior.
- Session-level reply dedupe: heartbeat persists the `dedupe_key` of every `task_terminal` event that produced a visible reply into `session.metadata["handled_terminal_dedupe_keys"]` (bounded, string-only, de-duplicated). A later delivery of the same `dedupe_key` is consumed silently (`pop_many` + `mark_task_terminal_outbox_delivered`) without running the agent or re-publishing `ceo.reply.final`. This guards the crash window between reply persist and queue-pop/outbox-ack where the same terminal could otherwise be processed twice and produce two final replies on two different heartbeat turns.
- The heartbeat final event (`ceo.reply.final`) carries the same canonical-context fields as the user-lane relay: it includes `canonical_context` + `canonical_context_delta` only when the just-persisted reply added new stage progress relative to the previous persisted assistant message; when the delta is empty both fields are omitted so the browser renders a plain reply bubble and never re-submits accumulated stage rails.
- The task-terminal event payload has two result lanes that maintainers should keep separate:
  - `terminal_*` still describes the true terminal node for the task-terminal event. When final acceptance fails, this remains the acceptance node result.
  - `root_output` / `root_output_ref` carries the root execution deliverable separately so heartbeat can still show the main agent the full root-node final output even while the terminal node is `acceptance`.
- Heartbeat task-terminal prompt assembly should therefore render both pieces when final acceptance fails:
  - the acceptance-node result (`Result output`, `Result check`, `Result failure reason`)
  - the root execution deliverable (`Execution output`, `Execution output ref`)
- Compact task-memory / task-ledger summaries may continue to store only preview-sized excerpts. The full root execution output requirement applies to the heartbeat event bundle that the main agent reads, not to every later summary surface.

## What Heartbeat Does Not Own

The CEO inline tool reminder lane is not a heartbeat turn.

- It does not enqueue a heartbeat event.
- It does not call `RuntimeAgentSession.prompt(...)`.
- It does not acquire the normal turn lock or enter the heartbeat running gate.
- It does not write transcript history, canonical context, or persistent session state.

This distinction matters when debugging long-running CEO direct tools: a reminder event is not evidence that a new internal turn happened.

## CEO Inline Tool Reminder Sidecar

CEO frontdoor direct long-running tools use a live-only sidecar reminder lane.

- Inline executions register in `InlineToolExecutionRegistry`, not in the detached `ToolExecutionManager` background-execution path.
- Reminder windows are fixed at `30 / 60 / 120 / 240 / 600` seconds, repeating every 600 seconds after the 600-second window.
- Argument-owned timeout opt-out: if the normalized top-level tool arguments already contain a timeout-bearing key such as `timeout_seconds`, the runtime skips reminder-sidecar stop/continue decisions and leaves timeout ownership to the tool itself.
- When the main turn has an authoritative CEO actual-request JSON, `CeoToolReminderService` reuses that saved `request_messages` / `tool_schemas` / `prompt_cache_key` / `parallel_tool_calls` scaffold as the provider-facing cache prefix and appends only live reminder-tail messages; otherwise it falls back to a read-only `CeoMessageBuilder.build_for_ceo(..., ephemeral_tail_messages=...)` rebuild.
- The sidecar reuses the CEO main model binding, but its decision channel is text-only (`STOP` / `CONTINUE`); even when it reuses the main turn's full provider-visible tool bundle for cache-prefix stability, it must not execute arbitrary returned tool calls.
- The reminder decision is observation-aware before the model is consulted: the sidecar reads the current tool name, normalized arguments, and the latest live `sidecar_observation` payload when available. `sidecar_observation` is a generic progress-side channel; `agent_browser` is the first producer. See `web-and-admin.md`「CEO Live Tool Reminder Contract」 for UI rendering of reminder events.
- Reminder labels remain live-only event data and must not be persisted into transcript, canonical context, or history injection.
- Reminder snapshots may carry the current visible stage view, compression progress, hydrated tools, selection debug, and actual-request pointer for cache forensics, but there is no semantic/global-summary persistence lane associated with reminder handling.

## Reminder Failure Semantics

- If the reminder model call fails, times out, or returns unusable output, the default decision is `unavailable`; `unavailable` reminders do not stop the tool and do not interrupt the main turn.
- Only an explicit sidecar stop decision is allowed to stop the inline execution.
- If the latest observation already shows a concrete hard tool failure, the sidecar keeps waiting so the tool can complete and surface its native error rather than converting that state into a timeout stop; the same applies when the observation shows positive progress such as a current URL, page title, or explicit progress marker.

## Timeout Stop Contract

When the sidecar stops a running CEO direct tool, the stop flows back into the main turn as an ordinary tool failure, not a silent cancel.

- The registry stores `InlineToolStopDecisionMetadata` with `reason_code=sidecar_timeout_stop`, which the direct tool completion path normalizes into a `tool_error` / `status=error` result visible to the main turn.
- The error text must include the tool name, elapsed runtime, reminder count, and the fact that the stop came from a sidecar timeout decision — e.g., `Error executing exec: stopped by sidecar timeout decision after 120.4s (2 reminders).`
- Sidecar timeout-stop targets only the inline tool execution's child cancellation token; it must not mark the whole visible CEO turn as cancelled or cause later tool calls in that same turn to fail immediately.
- If the tool finishes successfully before the stop lands, the runtime clears the timeout-stop metadata and preserves the successful result.

## Web/UI Boundary

- The sidecar publishes `ceo.tool.reminder` live websocket events.
- Browsers may keep these events entirely non-visual; the current CEO frontend no longer renders the reminder label as a visible text block under the pending turn.
- They do not become `snapshot.ceo` messages and are not restored on refresh or reconnect.

When debugging a “reminder appeared but the transcript stayed clean” report, that is the intended contract.
