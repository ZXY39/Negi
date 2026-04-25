# G3KU Architecture Docs

Start here when you are new to the repository or when a change crosses subsystem boundaries.

## Reading Order

1. `runtime-overview.md`
2. `context-and-cache-troubleshooting.md` when the change touches prompt caching, context retention, append-only request growth, or request artifact forensics
3. `tool-and-skill-system.md`
4. `web-and-admin.md`
5. `heartbeat-system.md` when the change touches heartbeat, long-running CEO tool wakeups, or live reminder behavior
6. `config-and-models.md` when the change touches runtime config, provider/model routing, or model bindings
7. `china-channels.md` when the change touches channel runtime or the Python/Node bridge

## Topic Guide

- `runtime-overview.md`
  Use for session lifecycle, frontdoor/runtime flow, tool execution flow, and cross-module runtime behavior.
- `context-and-cache-troubleshooting.md`
  Use for prompt cache misses, context shrink/continuity regressions, actual-request artifact forensics, and before changing node or CEO context strategies.
- `tool-and-skill-system.md`
  Use for candidate tools, hydrated tools, skill loading, tool RBAC, and runtime tool contracts.
- `web-and-admin.md`
  Use for websocket contracts, frontend/backend responsibility boundaries, and operator-visible UI behavior.
- `heartbeat-system.md`
  Use for heartbeat turns, task-terminal/stall wakeups, and the boundary between heartbeat and the CEO inline tool reminder sidecar.
- `config-and-models.md`
  Use for config source-of-truth rules and role-to-model resolution.
- `china-channels.md`
  Use for session key rules and the channel bridge boundary.

## Current Maintenance Note

The CEO long-running direct-tool reminder lane is now documented as a live-only sidecar, not as a heartbeat turn. If you are debugging reminder UI, timeout-stop failures, or `ceo.tool.reminder`, read `runtime-overview.md`, `web-and-admin.md`, and `heartbeat-system.md` together.

Node execution and `message_distribution` turns now also have a final send-side token preflight before provider dispatch. If you are debugging node cache misses, restart-seed continuity, or “distribution resumed but no new LLM request” symptoms, read `runtime-overview.md` and `context-and-cache-troubleshooting.md` together before changing node prompt assembly.

That final token preflight is no longer estimate-only. Node runtime and CEO/frontdoor now share the same provider-agnostic ground-truth contract: normalize usage first, treat `effective_input_tokens = input_tokens + cache_hit_tokens`, prefer `max(preview_estimate, usage_plus_delta)` when continuity is provable, and attempt compaction before failing when the hybrid estimate is already over the window. For providers that report cache hits as a nested breakdown of total input tokens (for example `input_tokens_details.cached_tokens` or `prompt_tokens_details.cached_tokens`), normalize `input_tokens` back to the uncached lane before computing `effective_input_tokens`. If the provider omits input-side usage entirely, persist `observed_input_truth` from the final preflight estimate instead of dropping the truth record. If you are debugging a “why did this miss compression?” or “why did the UI show a different context load than runtime used?” report, start with `runtime-overview.md` and `context-and-cache-troubleshooting.md` before changing thresholds.

Task-wide append-notice delivery now uses a barrier / drain / distribute / resume flow. If you are debugging “root got the notice but children kept running”, “why is `waiting_children` replay still happening?”, or “why does the task tree still show a banner after distribution finished?”, read `runtime-overview.md`, `web-and-admin.md`, and `operations-and-maintenance.md` together.

Execution/final-acceptance now also use a shared reflation handshake. If you are debugging “execution succeeded but vanished from the browser tree”, “why is an acceptance node visible before activation or hidden after rejection?”, or “why did final acceptance reject once and the task kept running?”, read `runtime-overview.md`, `web-and-admin.md`, and `operations-and-maintenance.md` together before changing task-tree projection or terminal-task handling.
