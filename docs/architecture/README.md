# G3KU Architecture Docs

Start here when you are new to the repository or when a change crosses subsystem boundaries.

## Reading Order

1. `runtime-overview.md`
2. `operations-and-maintenance.md` when you need to run, debug, or deploy the system
3. `context-and-cache-troubleshooting.md` when the change touches prompt caching, context retention, append-only request growth, or request artifact forensics
4. `tool-and-skill-system.md`
5. `web-and-admin.md`
6. `heartbeat-system.md` when the change touches heartbeat, long-running CEO tool wakeups, or live reminder behavior
7. `config-and-models.md` when the change touches runtime config, provider/model routing, or model bindings
8. `china-channels.md` when the change touches channel runtime or the Python/Node bridge

## Topic Guide

- `runtime-overview.md`
  Use for session lifecycle, frontdoor/runtime flow, tool execution flow, and cross-module runtime behavior.
- `operations-and-maintenance.md`
  Use for startup workflows, troubleshooting order, high-risk change types, memory queue/reset workflows, and Docker deployment.
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

## Debugging Entry Points

- Reminder UI or `ceo.tool.reminder` timeout-stop failures → `heartbeat-system.md` (+ `web-and-admin.md` for UI rendering)
- Node cache misses, restart-seed continuity, token preflight/compression questions → `context-and-cache-troubleshooting.md` (+ `runtime-overview.md`)
- Append-notice delivery, `waiting_children` replay, task-tree banner after distribution → `runtime-overview.md` + `operations-and-maintenance.md`
- Execution/final-acceptance reflation (node vanishing from browser tree, acceptance visibility) → `runtime-overview.md` + `web-and-admin.md`
- Multimodal image not reaching model or fabricated image content → `runtime-overview.md` + `china-channels.md` (channel inbound) or `web-and-admin.md` (web upload/reopen)
- Broken image icons, file-route 400s, snapshot path mismatch → `web-and-admin.md` "Inline Markdown Image Rendering Contract"

## Maintenance Rules

These rules prevent the docs from re-accumulating redundancy. Every edit to this directory must follow them:

1. Update in place, never append. Rewrite the section that owns the topic; never add trailing addendum sections ("X Update", "X Notes").
2. One contract, one home. State each contract only in its owning doc below; everywhere else use a pointer: `See <doc> "<topic>"` / `详见 <doc>「<topic>」`.
3. Pointers name topics, never section numbers.
4. Present tense only. No "now / no longer / previously / 现在 / 不再 / 曾经" — that is changelog language.
5. Superseded text is deleted outright, never left as "obsolete notes".
6. Word budgets: `runtime-overview` 9,000 / `web-and-admin` 8,500 / `tool-and-skill-system` 5,000 / `context-and-cache-troubleshooting` 4,000 / `operations-and-maintenance` 3,000 / `heartbeat-system` 2,600 / `config-and-models` 2,000 / `china-channels` 800. Over budget → condense before adding.

## Topic Ownership

| Topic | Owning doc |
|---|---|
| Runtime layering, message execution chain, session/task relationship, distribution / append-notice contract, provider timeout boundary | `runtime-overview.md` |
| Frontdoor context compression contract (`token_compression` / `stage_compaction`) | `runtime-overview.md` |
| Memory queue state/file semantics (`runtime-overview`); queue/reset operator workflows (`operations-and-maintenance`) | both, split as shown |
| Heartbeat continuation contract, cron at-most-once delivery, reminder sidecar decision semantics, timeout stop, task terminal repair | `heartbeat-system.md` |
| Tool/skill four concepts, candidate→callable chain, Tool Admin RBAC semantics, duplicate-call guard, catalog freshness | `tool-and-skill-system.md` |
| Actual-request forensics, append-only rule, cache-miss triage, token preflight diagnostics | `context-and-cache-troubleshooting.md` |
| Websocket/UI contracts, composer/media rendering, image upload gating, container deployment | `web-and-admin.md` |
| Config schema, hot refresh, model bindings, secret location, deployment unlock | `config-and-models.md` |
| China channel registry, session key rules, Python/Node bridge, canonical channel id list | `china-channels.md` |
| Startup/deploy/troubleshooting order, memory CLI, Docker compose | `operations-and-maintenance.md` |
