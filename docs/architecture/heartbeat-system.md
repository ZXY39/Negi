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
- The runtime appends two hidden durable messages before the model call:
  - a `system` rule message
  - a `user` event-bundle message
- Those internal prompt messages must be persisted with `prompt_visible=true`, `ui_visible=false`, and an `internal_prompt_kind` that distinguishes heartbeat vs cron rule/event records.
- Because the request is append-only against the previous authoritative scaffold, heartbeat/cron now share the same prompt-cache family, token-preflight, token-compression, and continuity rules as ordinary CEO/frontdoor turns.
- Silent `HEARTBEAT_OK` remains the only live-only exception. If an internal turn produces a real assistant reply, that reply is durable transcript history and should remain visible to later prompt assembly.

## Persistence And UI Boundary

- Hidden heartbeat/cron rule + event-bundle messages are durable prompt history. They should participate in later prompt assembly, request artifacts, completed continuity sidecars, and compression just like any other prompt-visible message.
- Frontend transcript views, session preview text, session message counts, and `snapshot.ceo.messages` must hide those internal prompt messages by filtering `ui_visible=false`, not by assuming every internal turn is transcript-hidden.
- Heartbeat/cron assistant replies, tool calls, tool results, and stage/compression traces remain ordinary visible turn output unless the turn ends with the silent `HEARTBEAT_OK` ACK path.
- Manual pause during a running heartbeat/cron turn still goes through the ordinary `client.pause_turn` path. The backend should treat that internal turn as the current active turn rather than as a side lane.

## Task Terminal Repair Contract

- Task-terminal heartbeat only repairs or produces the session reply for an existing terminal event.
- It no longer auto-runs `continue_task`, no longer creates replacement tasks, and no longer retries failed tasks in place.
- If a task still needs more work after terminalization, that must come from a later explicit frontdoor/user decision, typically via `create_async_task`.

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
