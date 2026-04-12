---
name: g3ku-architecture-maintenance
description: Maintain G3KU architecture docs in docs/architecture when code changes affect system understanding, runtime behavior, contracts, or maintenance workflow.
---

# G3KU Architecture Maintenance

Use this skill whenever a change may affect how a future maintainer understands, operates, or safely modifies the system.

This skill is project-specific. Its purpose is not to produce release notes or changelogs. Its purpose is to keep `docs/architecture/` accurate for new maintainers.

## Goals

When invoked, do all of the following:

1. Decide whether architecture docs need updates
2. Identify which specific docs in `docs/architecture/` are affected
3. Update those docs to reflect the current behavior
4. Update `docs/architecture/README.md` if system navigation, subsystem boundaries, or reading order changed
5. Keep the docs explanatory and maintenance-oriented, not change-log oriented

## What Counts As Architecture-Relevant

You should update docs when changes affect any of these:

- Subsystem responsibilities
- Cross-module boundaries
- Runtime or execution flow
- Session, task, heartbeat, pause/resume, recovery, or continuation behavior
- Tool registry, skill loading, candidate pools, hydration, or callable tool rules
- Config structure, config path, model binding, runtime refresh behavior
- Web/API/backend integration
- China bridge or Python/Node interaction
- Startup, shutdown, deployment, troubleshooting, or operator workflow

You usually do not need updates for:

- Pure tests
- Small wording changes
- Tiny local fixes with no maintenance impact
- Internal refactors that preserve the same architecture and operator behavior

## Architecture Doc Map

Use this mapping to decide what to update.

### `docs/architecture/runtime-overview.md`

Update when touching:

- `g3ku/runtime/`
- `g3ku/session/`
- `g3ku/agent/loop.py`
- `g3ku/runtime/engine.py`
- `g3ku/runtime/bridge.py`
- `g3ku/runtime/session_agent.py`
- `main/runtime/`
- `main/service/runtime_service.py`

Focus on:

- Main execution chain
- Session/task relationship
- Frontdoor vs main runtime responsibilities
- Recovery, pause/resume, continuation, orchestration semantics

### `docs/architecture/heartbeat-system.md`

Update when touching:

- `g3ku/heartbeat/`
- `g3ku/runtime/prompts/heartbeat_rules.md`
- Heartbeat enqueue/ack/reply behavior
- Task terminal/stall backflow into sessions

Focus on:

- Event types
- Wake/run loop behavior
- Internal prompt flow
- User-visible reply behavior

### `docs/architecture/tool-and-skill-system.md`

Update when touching:

- `g3ku/agent/tools/`
- `g3ku/agent/skills.py`
- `g3ku/runtime/context/`
- `g3ku/runtime/frontdoor/` logic related to tool/skill visibility
- `main/service/runtime_service.py` logic for visible tools, candidate tools, hydration, `load_tool_context`, `load_skill_context`
- Resource/tool/skill registry behavior

Focus on:

- Candidate vs callable distinction
- Fixed builtin tools
- Skill loading semantics
- Hydration and next-turn visibility
- RBAC/governance interaction

### `docs/architecture/web-and-admin.md`

Update when touching:

- `g3ku/web/`
- `g3ku/shells/web.py`
- `main/api/`
- `g3ku/runtime/api/`
- Frontend/backend integration or worker orchestration from web

Focus on:

- Startup flow
- Runtime attachment
- Major API surfaces
- Frontend/backend responsibilities

### `docs/architecture/config-and-models.md`

Update when touching:

- `g3ku/config/`
- `g3ku/llm_config/`
- Runtime config refresh
- Provider/model route resolution
- Secret handling
- Config schema and migration behavior

Focus on:

- Config source of truth
- Runtime refresh rules
- Role-to-model resolution
- Where secrets really live

### `docs/architecture/china-channels.md`

Update when touching:

- `g3ku/china_bridge/`
- `subsystems/china_channels_host/`
- Channel registry
- Session key rules
- Python/Node bridge protocol or startup flow

Focus on:

- Boundary between Python and Node
- Inbound/outbound flow
- Session key format
- Current maintenance risks and registry truth sources

### `docs/architecture/operations-and-maintenance.md`

Update when touching:

- Startup workflow
- Worker mode behavior
- Required operator commands
- Troubleshooting flow
- Verification steps maintainers should run

Focus on:

- How to run the system
- Where to look when it breaks
- What changes are high risk

### `docs/architecture/README.md`

Update this file if any of the following change:

- A new architecture topic doc is added
- A topic doc is removed or renamed
- The recommended reading order changes
- The system-level decomposition changes
- A maintenance entrypoint or major subsystem boundary changes

## How To Update The Docs

When updating a doc:

1. Write for a new maintainer, not for the author of the change
2. Explain the stable concepts first, then the important caveats
3. Prefer responsibilities, flows, boundaries, and risk points
4. Do not turn the doc into a file inventory or changelog
5. Preserve concise structure and maintenance usefulness

Good additions include:

- “This subsystem is responsible for…”
- “A typical flow is…”
- “The key boundary is…”
- “New maintainers often misread…”
- “If this breaks, check…”

Avoid:

- Commit-style summaries
- Temporary implementation notes
- Deep code dump details that will rot quickly
- Bullet lists of every file in a directory unless they serve the maintenance goal

## Update Decision Checklist

Before finishing, answer these:

1. Did the change alter how a maintainer should understand the subsystem?
2. Did it alter a runtime path, boundary, contract, or operator workflow?
3. Does an existing architecture doc now describe outdated behavior?
4. Should the top-level `README.md` navigation also change?

If any answer is “yes”, update the relevant docs.

## Completion Checklist

Before declaring completion:

1. Re-read the changed code paths
2. Re-read the affected architecture doc sections
3. Confirm the docs describe the new behavior accurately
4. Confirm `docs/architecture/README.md` still points to the right topic docs
5. In the final summary, list which architecture docs were updated

## Expected Output In Final Summary

When this skill leads to doc changes, the final summary should mention:

- Which architecture docs were updated
- Why they were updated
- Whether `README.md` was also updated

When no doc changes were needed, say so explicitly and state why the change was not architecture-relevant.
