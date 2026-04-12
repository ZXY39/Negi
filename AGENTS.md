# G3KU Agent Instructions

This repository is designed to be maintainable by agents with little or no prior context. Treat this file as the default entrypoint for understanding how to work in the codebase.

## Primary Goal

Before changing code, build a maintenance-level understanding of the relevant subsystem from the architecture docs in `docs/architecture/`.

After changing code, maintain the matching architecture docs when the change affects system understanding, runtime behavior, contracts, or operator workflow.

## Required Reading Order

Before any implementation work:

1. Read `docs/architecture/README.md`
2. Then read the topic docs relevant to the area you are touching

Use this mapping:

- Runtime, session, frontdoor, main runtime, task execution:
  `docs/architecture/runtime-overview.md`
- Heartbeat, heartbeat prompts, session wakeup/reply flow:
  `docs/architecture/heartbeat-system.md`
- Tool registry, skills, candidate pools, tool hydration, context selection:
  `docs/architecture/tool-and-skill-system.md`
- Web server, admin APIs, frontend/runtime integration:
  `docs/architecture/web-and-admin.md`
- Config loading, runtime refresh, model bindings, provider resolution:
  `docs/architecture/config-and-models.md`
- China bridge, channel runtime, Python/Node boundary:
  `docs/architecture/china-channels.md`
- Startup, operator workflow, troubleshooting, maintenance expectations:
  `docs/architecture/operations-and-maintenance.md`

If a task spans multiple subsystems, read all relevant docs before changing code.

## Required Documentation Workflow

If a change affects any of the following, you must use the project skill `g3ku-architecture-maintenance` before considering the task complete:

- Architecture boundaries
- Runtime flow
- Module responsibilities
- Session/task lifecycle behavior
- Heartbeat behavior
- Tool or skill visibility rules
- Candidate pool or hydration behavior
- Config fields, config paths, or model binding behavior
- API contracts or operator-visible workflows
- China bridge or channel behavior
- Troubleshooting guidance or startup flow

The skill lives at:

- `skills/g3ku-architecture-maintenance/SKILL.md`

Use it to determine:

- Whether docs must be updated
- Which docs must be updated
- Whether `docs/architecture/README.md` must also be updated

## Completion Requirements

Do not consider a task complete until you have done all of the following:

1. Checked whether the architecture docs are affected
2. Used the `g3ku-architecture-maintenance` skill when the change is architecture-relevant
3. Updated the relevant docs when needed
4. Mentioned the updated documentation files in your final summary

If no architecture docs were updated, explicitly state why they were not needed.

## Cases That Usually Do Not Require Architecture Doc Updates

These usually do not require updating `docs/architecture/` unless they alter maintenance understanding:

- Pure test additions
- Small text or wording changes
- Narrow bug fixes that do not change runtime behavior or contracts
- Internal refactors that do not change subsystem responsibilities, flow, or operator guidance

## Guidance For New Agents

If you are new to this repository, do not start by reading files at random. Start from:

1. `docs/architecture/README.md`
2. The subsystem docs listed above
3. Only then drill into source files

When in doubt, prefer preserving and improving the architecture docs rather than leaving behavior undocumented.

## Fast Onboarding Path For A New Agent

If you are taking over this repository for the first time and need the shortest possible path to useful context, follow this order:

1. Read `docs/architecture/README.md`
2. Read `docs/architecture/runtime-overview.md`
3. Read `docs/architecture/tool-and-skill-system.md`
4. Read `docs/architecture/web-and-admin.md`
5. If the task touches heartbeat, config/models, or China bridge, read those topic docs before opening source files
6. After the docs pass, inspect the concrete entrypoints:
   - `g3ku/cli/commands.py`
   - `g3ku/shells/web.py`
   - `g3ku/runtime/session_agent.py`
   - `main/service/runtime_service.py`

If the task is broad or the affected area is still unclear after this pass, use the project onboarding skill:

- `skills/g3ku-project-onboarding/SKILL.md`

Use that skill to build a quick mental model before making changes.
