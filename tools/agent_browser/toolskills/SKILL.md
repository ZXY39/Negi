---
name: agent_browser
description: Use this tool when a task needs browser automation through the external agent-browser CLI, including navigating websites, taking snapshots, clicking elements, filling forms, capturing screenshots, persisting sessions, or connecting to an existing browser via CDP/provider features.
allowed-tools: Bash(agent-browser:*), Bash(npx agent-browser:*), Bash(pnpm dlx agent-browser:*), Bash(cmd /c agent-browser:*), Bash(powershell -Command agent-browser:*)
---

# agent_browser

## What this tool is

`agent-browser` is an **external** tool registration for the upstream GitHub project:

- Repository: `https://github.com/vercel-labs/agent-browser`
- Package: `agent-browser`
- Current documented upstream version at integration time: `0.20.13`
- Delivery model in this repo: **external**, not vendored

This repository does **not** copy the upstream source into `tools/agent_browser/main/`. Instead, it registers how to install, invoke, verify, and maintain the tool safely.

## Why external mode is the right fit

Choose external mode because the upstream project is:

1. A fast-moving third-party repository with its own release tags and changelog.
2. A multi-platform CLI distributed through package managers and compiled native binaries.
3. Better maintained by reinstalling/upgrading from upstream than by copying source into this repo.
4. Usable directly as a shell tool once installed (`agent-browser ...`).

Use internal mode only if this repository later adds a **local wrapper implementation** or API bridge under `tools/agent_browser/main/` for platform-specific orchestration. That is not necessary for the current integration goal.

## Install

Pick one supported install path.

### Option A: npm global install

```bash
npm install -g agent-browser
agent-browser install
```

Notes:

- Recommended by upstream for general CLI usage.
- `agent-browser install` downloads Chrome for Testing when needed.
- On Linux, if dependencies are missing, try:

```bash
agent-browser install --with-deps
```

### Option B: project-local npm install

```bash
npm install agent-browser
npx agent-browser install
```

Use this if you want the project to pin a version in `package.json` instead of relying on a global install.

### Option C: Homebrew on macOS

```bash
brew install agent-browser
agent-browser install
```

### Option D: Cargo

```bash
cargo install agent-browser
agent-browser install
```

### Option E: build from source outside this workspace

```bash
git clone https://github.com/vercel-labs/agent-browser.git
cd agent-browser
pnpm install
pnpm build:native
pnpm link --global
agent-browser install
```

Use this only when you explicitly need source-level debugging, local patching, or a nonstandard build flow.

## Verify installation

Run at least:

```bash
agent-browser --version
agent-browser --help
```

Then do a smoke test:

```bash
agent-browser open https://example.com
agent-browser wait --load networkidle
agent-browser snapshot -i
agent-browser close
```

Expected outcome:

- Version command succeeds.
- Browser opens and loads the page.
- Snapshot returns accessible element refs such as `@e1`, `@e2`.

## Core usage pattern

A reliable browser automation loop is:

1. Open a page.
2. Wait for load or relevant content.
3. Snapshot to obtain fresh refs.
4. Interact using refs.
5. Re-snapshot after page changes.

Example:

```bash
agent-browser open https://example.com/login
agent-browser wait --load networkidle
agent-browser snapshot -i
agent-browser fill @e1 "user@example.com"
agent-browser fill @e2 "secret"
agent-browser click @e3
agent-browser wait --url "**/dashboard"
agent-browser snapshot -i
```

## High-value commands

### Navigate and inspect

```bash
agent-browser open <url>
agent-browser get url
agent-browser get title
agent-browser snapshot -i
agent-browser close
```

### Interaction

```bash
agent-browser click @e1
agent-browser fill @e2 "text"
agent-browser type @e2 "text"
agent-browser select @e3 "option"
agent-browser press Enter
agent-browser hover @e4
agent-browser scroll down 500
```

### Capture artifacts

```bash
agent-browser screenshot
agent-browser screenshot --full
agent-browser screenshot --annotate
agent-browser pdf output.pdf
```

### Waiting

```bash
agent-browser wait 2000
agent-browser wait --load networkidle
agent-browser wait --text "Welcome"
agent-browser wait "#spinner" --state hidden
```

### Sessions, auth, and persistence

```bash
agent-browser --profile ~/.myapp open https://app.example.com
agent-browser --session-name myapp open https://app.example.com
agent-browser state save ./auth.json
agent-browser state load ./auth.json
```

### Existing browser / remote connectivity

When supported by the installed upstream version, use direct connection options such as CDP or provider features instead of launching a fresh local browser.

Examples of the kinds of workflows the upstream tool supports:

```bash
agent-browser get cdp-url
agent-browser connect <port>
agent-browser --auto-connect snapshot -i
```

Before relying on provider-specific flags in automation, confirm the exact flags and environment variables with:

```bash
agent-browser --help
```

because upstream may add or rename remote connectivity options between releases.

## Operational advice for agents

- Prefer **refs from `snapshot -i`** over brittle CSS selectors.
- Re-run `snapshot -i` after navigation, modal open/close, or major DOM changes.
- Use `wait --load networkidle`, `wait --url`, or `wait --text` to reduce timing flakiness.
- Store login state in named sessions or profile directories for repeat tasks.
- Treat saved auth/state files as secrets.
- If you only need a one-off invocation without global install, `npx agent-browser ...` is acceptable.

## Security and safety notes

This tool can:

- open arbitrary websites,
- submit forms,
- use authenticated browser state,
- read or write persistent session/auth data,
- download files,
- interact with local or remote browser/provider endpoints.

Therefore:

- never commit auth state, profiles, or secrets into the repository;
- prefer encrypted or ephemeral storage when available;
- review remote provider settings before use in sensitive environments.

## Maintenance workflow in this repository

When updating this tool registration:

1. Inspect `tools/agent_browser/resource.yaml`.
2. Check upstream tags/releases and version metadata.
3. Reinstall or upgrade the external tool outside this repository.
4. Verify with `agent-browser --version` and a small smoke test.
5. Update:
   - `tools/agent_browser/resource.yaml`
   - `tools/agent_browser/toolskills/SKILL.md`

## Suggested upgrade procedure

```bash
git ls-remote --tags https://github.com/vercel-labs/agent-browser.git
agent-browser --version
```

Then compare the installed version with upstream `package.json` / release tags / changelog.

If upgrading via npm:

```bash
npm install -g agent-browser@latest
agent-browser install
agent-browser --version
```

If upgrading via Homebrew or Cargo, use the corresponding package manager update flow and then rerun the same smoke tests.

## Recommended reusable invocation patterns

### Quick page exploration

```bash
agent-browser open https://example.com && agent-browser wait --load networkidle && agent-browser snapshot -i
```

### Annotated screenshot for debugging

```bash
agent-browser open https://example.com && agent-browser wait --load networkidle && agent-browser screenshot --annotate
```

### Form fill workflow

```bash
agent-browser open https://example.com/form
agent-browser wait --load networkidle
agent-browser snapshot -i
agent-browser fill @e1 "Jane Doe"
agent-browser fill @e2 "jane@example.com"
agent-browser click @e3
```

### Session reuse

```bash
agent-browser --session-name myapp open https://app.example.com
```

## Maintenance recommendations

- Keep this registration in **external** mode unless there is a concrete need for a local wrapper.
- Track upstream releases by tag and changelog, not by memory.
- Re-verify remote/CDP/provider options on every major or minor upgrade.
- If this repo later needs a stable higher-level interface, add a thin local wrapper rather than vendoring the whole upstream repository.
