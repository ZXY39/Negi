# Negi Agent Instructions

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
- Task message distribution / append notices, acceptance handoff, spawn review, node pause-resume-cancel, shutdown pause and auto-resume:
  `docs/architecture/main-task-runtime.md`
- Heartbeat, heartbeat prompts, session wakeup/reply flow:
  `docs/architecture/heartbeat-system.md`
- Tool registry, skills, candidate pools, resource reload and RBAC:
  `docs/architecture/tool-and-skill-system.md`
- Tool hydration and promotion, parameter-error guidance, universal tool timeout, stage gating:
  `docs/architecture/tool-hydration-and-callable-chain.md`
- Web server, admin APIs, frontend/runtime integration:
  `docs/architecture/web-and-admin.md`
- Local speech-to-text: composer mic, inbound channel voice, `stt` config, whisper.cpp provisioning:
  `docs/architecture/speech-to-text.md`
- Config loading, runtime refresh, model bindings, provider resolution:
  `docs/architecture/config-and-models.md`
- External bridge API (`/api/v1`), external sessions, outbound routing, built-in official QQ adapter:
  `docs/architecture/external-agent-api.md`
- OpenAI-compatible endpoint, MCP stdio gateway, agent-facing integrations:
  `docs/architecture/agent-gateway.md`
- Startup, operator workflow, troubleshooting, maintenance expectations:
  `docs/architecture/operations-and-maintenance.md`

If a task spans multiple subsystems, read all relevant docs before changing code.

## Required Documentation Workflow

If a change affects any of the following, you must use the project skill `negi-architecture-maintenance` before considering the task complete:

- Architecture boundaries
- Runtime flow
- Module responsibilities
- Session/task lifecycle behavior
- Heartbeat behavior
- Tool or skill visibility rules
- Candidate pool or hydration behavior
- Config fields, config paths, or model binding behavior
- API contracts or operator-visible workflows
- External bridge, channel, or agent gateway behavior
- Troubleshooting guidance or startup flow

The skill lives at:

- `skills/negi-architecture-maintenance/SKILL.md`

Use it to determine:

- Whether docs must be updated
- Which docs must be updated
- Whether `docs/architecture/README.md` must also be updated

## Code Quality Workflow

Before committing code changes:

- Run lint: `python -m ruff check .` (or `scripts/lint.sh` / `scripts/lint.ps1`).
- Run the pytest tests relevant to the change, at minimum the smoke subset (`python -m pytest tests/resources/test_resource_runtime_smoke.py -q`) plus related tests.
- Add new tests directly under `tests/`, which is tracked by default.

## Completion Requirements

Do not consider a task complete until you have done all of the following:

1. Checked whether the architecture docs are affected
2. Used the `negi-architecture-maintenance` skill when the change is architecture-relevant
3. Updated the relevant docs when needed
4. Mentioned the updated documentation files in your final summary

If no architecture docs were updated, explicitly state why they were not needed.

## Cases That Usually Do Not Require Architecture Doc Updates

These usually do not require updating `docs/architecture/` unless they alter maintenance understanding:

- Pure test additions
- Small text or wording changes
- Narrow bug fixes that do not change runtime behavior or contracts
- Internal refactors that do not change subsystem responsibilities, flow, or operator guidance

## Brand Name vs Frozen Identifiers

The product is displayed as **Negi**, the repository is `ZXY39/Negi`, and the operator-facing command and entry scripts are `negi` / `negi web` / `start-negi.*` / `negi.ps1` / `negi_bootstrap.py` / `uninstall.ps1` / `uninstall.sh`. The code keeps the `g3ku` spelling. Every entry below is a live contract, not branding — do not rename one for consistency:

- The repository path `ZXY39/Negi` and the URLs and directory built from it: `$RepoName` / `REPO_NAME` in `install.ps1` / `install.sh`, and the default install directory `~/Negi`. `G3KU-Agent` must not appear as a name: GitHub drops an old-name redirect as soon as that name is taken, and installed devices check and upgrade through their own `origin` (`g3ku/update_check.py` `git ls-remote --tags origin`, `install.ps1` `git fetch origin`), so they would silently stop receiving updates.
- The Python package directory `g3ku/` and therefore every `python -m g3ku` form. This is not only an import path: it is the cmdline shape the stale-process reaper matches (`start-negi.ps1` / `start-negi.sh` test `-m\s+g3ku\s+web|worker`, and `g3ku/web/launcher.py::_web_runtime_cmdline_matches`), and `worker_control.py`, `update_apply.py` and `docker/*-entrypoint.sh` all spawn that shape. Renaming the package would disable the reaper silently.
- Context protocol markers, written brand-free: `[STAGE_COMPACT_V1]`, `[STAGE_EXTERNALIZED_V1]`, `[STAGE_RAW_V1]`, `[TOKEN_COMPACT_V2]`, `[NOTICE_TAIL_V1]`, and the patch artifact section markers `### PATCH_METADATA ###` / `### PATCH_DIFF ###`. The runtime also *reads* the `[G3KU_*]` spellings of those blocks — stored baselines, sidecars and request artifacts carry them — so never write that spelling, and do not delete those read tuples until the data rolls out. `[G3KU_SILENT` decides nothing: it is only stripped from outbound text.
- `G3KU_*` environment variables, including every config field, because the
  root settings model declares `env_prefix="G3KU_"` — the set is open-ended,
  not a list to migrate.
- The data root directory name `.g3ku`, because stored task rows and `config.json` hold absolute paths under it; renaming it is a data migration, not a rename.
- Outbound contract strings: `MODEL_ID = "g3ku"`, `owned_by`, the `g3ku` key in OpenAI-compatible responses, the MCP server name `negi` and the `negi_*` tool names (do not rename, and do not "align" them back to the package spelling), the `[g3ku]` prefix on External-API turn-status texts (`g3ku/runtime/api/openai_compat.py`, quoted verbatim by `docs/architecture/agent-gateway.md`), and the `x-g3ku-internal-token` header.
- Frontend storage keys such as `g3ku.audit.last-seen.v1`.

Treat any of the above as a breaking change requiring its own plan.

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
3. Read `docs/architecture/main-task-runtime.md` for anything on the task side (distribution, acceptance, node pause/resume, shutdown resume)
4. Read `docs/architecture/tool-and-skill-system.md`, plus `docs/architecture/tool-hydration-and-callable-chain.md` when the change touches hydration, timeouts, parameter errors or stage gating
5. Read `docs/architecture/web-and-admin.md`
6. If the task touches heartbeat, config/models, the external bridge API, the agent gateway, or speech-to-text, read those topic docs before opening source files
7. After the docs pass, inspect the concrete entrypoints:
   - `g3ku/cli/commands.py`
   - `g3ku/shells/web.py`
   - `g3ku/runtime/session_agent.py`
   - `main/service/runtime_service.py`

If the task is broad or the affected area is still unclear after this pass, re-read `docs/architecture/README.md` (Topic Guide + Debugging Entry Points) and follow the owning doc for the affected subsystem before making changes.
