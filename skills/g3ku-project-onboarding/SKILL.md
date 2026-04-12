---
name: g3ku-project-onboarding
description: Quick onboarding workflow for agents that are new to the G3KU repository. Use this first to build a maintenance-level mental model before implementation.
---

# G3KU Project Onboarding

Use this skill when you are new to the G3KU repository or when the current task is broad enough that you need to quickly rebuild context before touching code.

This is a lightweight onboarding skill. It is not a substitute for the architecture maintenance skill. Its job is to help you understand the project fast enough to work safely.

## Goal

Build a practical working model of:

- what G3KU is
- where the major runtime boundaries are
- which subsystem the task belongs to
- which files you should read next

Do this before implementation if you do not already understand the area.

## Fast Onboarding Workflow

### Step 1: Read the architecture index

Always start with:

- `docs/architecture/README.md`

Your goal is to identify:

- the main runtime layers
- the split between `g3ku/`, `main/`, and `subsystems/`
- the likely subsystem for the current task

### Step 2: Read the minimum core topic docs

For a first-pass understanding, read these three docs in order:

1. `docs/architecture/runtime-overview.md`
2. `docs/architecture/tool-and-skill-system.md`
3. `docs/architecture/web-and-admin.md`

These three give the fastest useful map of the project for most tasks.

### Step 3: Read only the relevant extra topic docs

Then branch based on the task:

- Heartbeat, heartbeat prompts, session wake/reply flow:
  `docs/architecture/heartbeat-system.md`
- Config, models, provider binding, runtime refresh:
  `docs/architecture/config-and-models.md`
- China bridge, channel runtime, Python/Node boundary:
  `docs/architecture/china-channels.md`
- Startup, troubleshooting, operator workflow:
  `docs/architecture/operations-and-maintenance.md`

Do not bulk-read everything unless the task truly spans the whole system.

## Entry Files To Inspect After Docs

Once the docs are read, inspect these concrete entrypoints to anchor the mental model:

- `g3ku/cli/commands.py`
- `g3ku/shells/web.py`
- `g3ku/runtime/session_agent.py`
- `g3ku/runtime/manager.py`
- `g3ku/runtime/bridge.py`
- `main/service/runtime_service.py`

These files are the fastest path from architecture docs to actual execution flow.

## How To Classify The Task

Before coding, classify the task into one or more of these buckets:

- Runtime/session/frontdoor
- Task runtime/node execution
- Heartbeat
- Tool/skill/candidate/hydration
- Web/admin/API
- Config/models
- China bridge/channels
- Ops/startup/troubleshooting

Use that classification to decide which topic docs and source files to inspect next.

## What To Produce For Yourself

Before implementation, you should be able to answer these questions in 1-3 sentences each:

1. Which subsystem owns this behavior?
2. What is the main execution path for this task?
3. Which source files are the most likely places to modify?
4. Which architecture doc would become stale if this behavior changes?

If you cannot answer these, you are not onboarded enough yet. Read one more relevant topic doc before coding.

## When To Stop Onboarding

Stop the onboarding pass and move into implementation when:

- you can name the owning subsystem
- you know the likely entry file and integration file
- you know which architecture doc would need updates if behavior changes

Do not over-read the repo. The point is fast operational context, not exhaustive codebase study.

## Relationship To Other Skills

- Use this skill first when context is thin.
- Use `g3ku-architecture-maintenance` when your change affects architecture docs.
- This skill helps you understand the project.
- The architecture maintenance skill helps you keep the docs correct after changes.

## Final Reminder

Do not start from random deep files in `main/runtime/` or `subsystems/` before reading the architecture docs. In G3KU, this usually wastes time and leads to wrong mental models.
