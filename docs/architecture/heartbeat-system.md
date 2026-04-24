# G3KU Heartbeat System

This document describes the maintenance boundary around the Web CEO heartbeat path and the newer CEO inline tool reminder sidecar.

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

- Heartbeat and cron are no longer assembled through a separate short `ceo_heartbeat` request lane on the main CEO path.
- Each internal activation now resumes from the same session-owned `frontdoor_request_body_messages` / actual-request scaffold used by the next visible CEO turn.
- When that authoritative frontdoor baseline already carries prior frontdoor contract state, heartbeat and cron inherit the previous callable/candidate/hydrated/provider-tool/visible-skill state directly instead of rerunning tool or skill selection.
- If no authoritative frontdoor baseline exists yet, internal turns fall back to the ordinary CEO/frontdoor exposure assembly path for that round.
- Heartbeat still appends two hidden durable messages before the model call:
  - a `system` rule message
  - a `user` event-bundle message
- Cron now appends two hidden durable `system` messages:
  - a cron rule message
  - a structured cron event block
- Those internal prompt messages must be persisted with `prompt_visible=true`, `ui_visible=false`, and an `internal_prompt_kind` that distinguishes heartbeat vs cron rule/event records.
- Because the request is append-only against the previous authoritative scaffold, heartbeat/cron now share the same prompt-cache family, token-preflight, token-compression, and continuity rules as ordinary CEO/frontdoor turns.
- Silent `HEARTBEAT_OK` remains the only live-only exception. If an internal turn produces a real assistant reply, that reply is durable transcript history and should remain visible to later prompt assembly.

## Persistence And UI Boundary

- Hidden heartbeat/cron rule + event-bundle messages are durable prompt history. They should participate in later prompt assembly, request artifacts, completed continuity sidecars, and compression just like any other prompt-visible message.
- Frontend transcript views, session preview text, session message counts, and `snapshot.ceo.messages` must hide those internal prompt messages by filtering `ui_visible=false`, not by assuming every internal turn is transcript-hidden.
- Heartbeat/cron assistant replies, tool calls, tool results, and stage/compression traces remain ordinary visible turn output unless the turn ends with the silent `HEARTBEAT_OK` ACK path.
- Manual pause during a running heartbeat/cron turn still goes through the ordinary `client.pause_turn` path. The backend should treat that internal turn as the current active turn rather than as a side lane.

## Cron Reminder Contract

- Cron is now a structured reminder mechanism for the future agent, not a natural-language stop-condition engine.
- Cron `message` should be understood as the reminder instruction for the future agent, not as a ready-to-send user reply.
- Cron-internal turns are not a cron-only tool lane. When prior frontdoor contract state exists, they reuse the ordinary CEO/frontdoor tool exposure for the current role; the special-case is only that they bypass the normal “no valid stage => `submit_next_stage` only” shrink so a scheduled reminder can immediately call `create_async_task`, task query builtins, or other already-visible CEO tools.
- The prompt-side cron rule is now intentionally minimal: it tells the model that the reminder is an internal instruction, not a new user message, and that it should execute the reminded work immediately.
- The runtime no longer hard-codes a prompt-side ban on cron-tool mutations during structured reminder turns. If the reminded work itself is “send a plain-text reminder”, “create another cron”, or any other currently visible CEO action, the model may use the ordinary CEO tool surface for that work.
- Repetition is enforced by service-side counters:
  - `payload.max_runs`
  - `state.delivered_runs`
- A cron reminder only counts as delivered after the internal prompt is durably accepted by the runtime/session path.
- When `delivered_runs >= max_runs`, the cron service removes the job immediately and does not schedule another wakeup.
- One-shot `at` reminders are now also validated at creation time against the service clock. If the target timestamp is already in the past when `add_job()` runs, the cron service rejects creation immediately with `任务定时已过期，当前时间为<service-local time>，请立即执行或视情况废弃而不要创建过期任务` instead of storing a dormant expired job.
- If an old cron store uses the previous schema version, the runtime now drops those jobs instead of attempting migration; maintainers should treat this as an intentional semantic reset.

## Task Terminal Repair Contract

- Task-terminal heartbeat only repairs or produces the session reply for an existing terminal event.
- It no longer auto-runs `continue_task`, no longer creates replacement tasks, and no longer retries failed tasks in place.
- If a task still needs more work after terminalization, that must come from a later explicit frontdoor/user decision, typically via `create_async_task`.
- Task-terminal callback persistence and heartbeat queueing now have separate duplicate boundaries:
  - the outbox row is the durable callback boundary,
  - the heartbeat event queue only dedupes currently enqueued in-memory events.
- Because in-memory dedupe is transient, `/api/internal/task-terminal` must now also reject a repeated callback when the same outbox row is already `accepted=true` even if it is not yet `delivery_state=delivered`. Maintainers debugging "same failed task spawned two heartbeat replies" should inspect the task-terminal outbox row before blaming prompt behavior.
- The task-terminal event payload now has two result lanes that maintainers should keep separate:
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

CEO frontdoor direct long-running tools now use a live-only sidecar reminder lane.

- Inline executions register in `InlineToolExecutionRegistry`, not in the detached `ToolExecutionManager` background-execution path.
- Reminder windows are fixed at `30 / 60 / 120 / 240 / 600` seconds, and after the 600-second window they repeat every 600 seconds.
- When the main turn already has an authoritative CEO actual-request JSON, `CeoToolReminderService` reuses that saved `request_messages` / `tool_schemas` / `prompt_cache_key` / `parallel_tool_calls` scaffold as the provider-facing cache prefix and appends only live reminder-tail messages. It falls back to a read-only `CeoMessageBuilder.build_for_ceo(..., ephemeral_tail_messages=...)` rebuild only when no actual-request scaffold exists yet.
- The sidecar still reuses the CEO main model binding, but its decision channel is now text-only (`STOP` / `CONTINUE`). Even when it reuses the main turn's full provider-visible tool bundle for cache-prefix stability, it must not execute arbitrary returned tool calls.
- Reminder labels remain live-only event data and must not be persisted into transcript, canonical context, or history injection.

## Reminder Failure Semantics

- If the reminder model call fails, times out, or returns unusable output, the default decision is `unavailable`.
- `unavailable` reminders do not stop the tool and do not interrupt the main turn.
- Only an explicit sidecar stop decision is allowed to stop the inline execution.

## Timeout Stop Contract

When the sidecar stops a running CEO direct tool, the stop must flow back into the main turn as an ordinary tool failure, not as a silent cancel.

- The registry stores `InlineToolStopDecisionMetadata` with `reason_code=sidecar_timeout_stop`.
- The direct tool completion path normalizes that into a `tool_error` / `status=error` result visible to the main turn.
- The error text must include the tool name, elapsed runtime, reminder count, and the fact that the stop came from a sidecar timeout decision.

Example shape:

`Error executing exec: stopped by sidecar timeout decision after 120.4s (2 reminders).`

If the tool finishes successfully before the stop lands, the runtime clears the timeout-stop metadata and preserves the successful result.

## Web/UI Boundary

- The sidecar publishes `ceo.tool.reminder` live websocket events.
- Browsers may keep these events entirely non-visual; the current CEO frontend no longer renders the reminder label as a visible text block under the pending turn.
- They do not become `snapshot.ceo` messages and are not restored on refresh or reconnect.

When debugging a “reminder appeared but the transcript stayed clean” report, that is the intended contract.

## Reminder Sidecar Update

The CEO inline reminder sidecar is still live-only, but the downstream long-context handoff model changed:

- Reminder labels must not be rendered into transcript history or durable CEO bubbles.
- Reminder snapshots may still carry the current visible stage view, compression progress, hydrated tools, selection debug, and actual-request pointer for cache forensics.
- There is no longer a semantic/global-summary persistence lane associated with reminder handling. Any older note that says heartbeat or reminder work is recovered through semantic summary should be treated as obsolete.
