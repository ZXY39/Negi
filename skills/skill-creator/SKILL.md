# Skill Creator

Create or update root-level resources under `skills/` and `tools/`.

## What To Build

Use this skill when the task is to add, revise, or migrate:
- a skill in `skills/<skill_id>/`
- a tool in `tools/<tool_id>/`
- a tool's bundled usage guide in `toolskills/`

The unified resource system reads metadata from `resource.yaml`, not from Markdown frontmatter.

## Canonical Layout

### Skill layout

```text
skills/
  <skill_id>/
    resource.yaml
    SKILL.md
    references/    # optional
    scripts/       # optional
    assets/        # optional
```

Rules:
- `resource.yaml` is the single metadata source.
- `SKILL.md` is plain Markdown only. Do not add YAML frontmatter.
- Put long reference material in `references/`, executable helpers in `scripts/`, and output assets in `assets/`.

### Tool layout

```text
tools/
  <tool_id>/
    resource.yaml
    toolskills/
      SKILL.md
      references/  # optional
      scripts/     # optional
      assets/      # optional
    main/
      tool.py
      ...
```

Rules:
- Tool root may contain only `resource.yaml`, `toolskills/`, and `main/`.
- `toolskills/` does not get its own `resource.yaml`.
- `main/tool.py` is the fixed entrypoint. Do not invent extra manifest entrypoint fields.
- Put the user-facing usage guide in `toolskills/SKILL.md`, not in `README.md`.

## Creation Workflow

1. Inspect nearby resources first.
   - Reuse existing `resource.yaml` structure, parameter style, and phrasing from similar skills or tools.
   - For tools, inspect both `resource.yaml` and `main/tool.py`.

2. Choose the resource id carefully.
   - Use lowercase letters, digits, and hyphens for skill ids.
   - Use lowercase letters, digits, and underscores for tool ids when matching existing tool naming.
   - Keep ids stable when updating an existing resource.

3. Write `resource.yaml` before writing content.
   - Put trigger and discovery metadata here.
   - For skills, include `trigger`, `requires`, `content`, and `exposure`.
   - For tools, include `description`, `parameters`, `permissions`, `requires`, `config_namespace`, `exposure`, and `toolskill.enabled`.

4. Write the body content.
   - For skills, keep `SKILL.md` procedural and concise.
   - For tools, write `toolskills/SKILL.md` so another agent knows when to call the tool, what each parameter means, and what pitfalls to avoid.

5. Implement code only when creating or updating a tool.
   - `main/tool.py` must expose either `build(runtime)` or `execute(...)`.
   - Prefer `build(runtime)` when the tool needs services, config slices, workspace paths, or other runtime objects.
   - Use `runtime.services`, `runtime.workspace`, `runtime.resource_root`, `runtime.main_root`, and `runtime.toolskills_root` instead of hardcoded paths.

6. Validate the result.
   - Confirm the resource can be discovered from root `skills/` or root `tools/`.
   - Confirm tool roots do not contain legacy `README.md`, `research/`, `tool.yaml`, or `capability.yaml`.
   - When possible, run a targeted smoke test or `ResourceManager` reload path.

## Authoring Guidelines

### Metadata first

Discovery depends on `resource.yaml`, so write descriptions there as if they were the trigger contract.

For skills:
- Put "what it does" and "when to use it" in `resource.yaml -> description`.
- Keep `trigger.keywords` minimal and specific.

For tools:
- Make `description` clear enough for tool selection.
- Keep parameter names stable and descriptions concrete.

### Keep Markdown lean

- Put only the operational workflow in `SKILL.md` or `toolskills/SKILL.md`.
- Move long schemas, examples, and vendor docs into `references/`.
- Do not duplicate the same detailed content across body and references.

### Avoid legacy patterns

Do not introduce or preserve:
- YAML frontmatter in `SKILL.md`
- `capability.yaml`
- `tool.yaml`
- root-level `README.md` as the authoritative tool guide
- root-level `research/` as the authoritative tool guide

Migrate those into:
- `resource.yaml`
- `SKILL.md`
- `toolskills/SKILL.md`
- `references/`

## Minimal Templates

### Skill `resource.yaml`

```yaml
schema_version: 1
kind: skill
name: example-skill
description: 描述该 skill 的作用与适用场景。
trigger:
  keywords: []
  always: false
requires:
  tools: []
  bins: []
  env: []
content:
  main: SKILL.md
  references: references
  scripts: scripts
exposure:
  agent: true
  org_graph: true
```

### Tool `resource.yaml`

```yaml
schema_version: 1
kind: tool
name: example_tool
description: 描述该 tool 的作用与调用时机。
config_namespace: ''
requires:
  tools: []
  bins: []
  env: []
permissions:
  network: false
  filesystem: []
parameters:
  type: object
  properties: {}
exposure:
  agent: true
  org_graph: true
toolskill:
  enabled: true
```

## Update Workflow

When updating an existing resource:
- preserve the existing id unless a rename is explicitly required
- inspect current `resource.yaml` and body files before editing
- migrate lingering legacy wording such as "capability" when it refers to a root resource
- keep changes scoped to the real trigger, workflow, or runtime contract

When migrating an old builtin or legacy resource:
- move metadata into `resource.yaml`
- move skill content into `SKILL.md`
- move tool guidance into `toolskills/SKILL.md`
- move implementation into `main/tool.py`
- delete the legacy parallel entrypoints after the new path works
