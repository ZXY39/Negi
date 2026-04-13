# G3KU Web And Admin Architecture

This document describes how the web shell, admin APIs, and browser runtime fit together for day-to-day maintenance.

## Responsibilities And Boundaries

- `g3ku/shells/web.py` owns web runtime startup and binds the backend service into HTTP routes.
- `g3ku/web/frontend/*` owns browser-side rendering, interaction logic, and shell state presentation.
- `main/api/*` and `g3ku/runtime/api/*` own backend contracts consumed by the frontend.
- The browser shell should present backend state; authoritative project/runtime state remains backend-owned.

When debugging behavior, first identify which side owns the state transition:

- If the issue is display text, interaction wiring, or DOM updates, start in `g3ku/web/frontend/*`.
- If the issue is data shape, status lifecycle, or permissions, start in API/runtime services.

## Frontend I18n Runtime And Language Switching

The frontend language switcher is architecture-relevant because it changes operator-visible workflow and UI state behavior.

### Core Runtime Pieces

- `g3ku/web/frontend/locales/zh-CN.js` and `g3ku/web/frontend/locales/en-US.js` register locale dictionaries into `window.G3KU_LOCALES`.
- `g3ku/web/frontend/i18n.js` exposes `window.G3KUI18n` and applies translations to:
  - `data-i18n` text content
  - `data-i18n-placeholder` placeholder text
  - `data-i18n-aria-label` accessibility labels
- Locale preference is persisted in browser storage under key `g3ku.ui.locale.v1`.

### Shell Integration Flow

1. `org_graph.html` loads locale files before `i18n.js` so dictionaries are available during i18n initialization.
2. `i18n.js` resolves locale from persisted value or fallback locale and applies translations.
3. The shell language `<select id="language-switch">` calls `window.G3KUI18n.setLocale(nextLocale)` on change.
4. `i18n.js` emits `g3ku:locale-changed` after successful locale changes.
5. Shell listeners update locale-linked UI state (for example, `<html lang=...>` and switcher selection sync).

### Maintenance Caveats

- Script order is contract-sensitive: locale dictionaries must load before `i18n.js`.
- New frontend copy should use translation keys rather than hardcoded language strings.
- If new controls need localized placeholders or ARIA labels, use the existing `data-i18n-*` attributes.
- Locale persistence is browser-local; no backend API currently stores per-user UI locale.

## Operator-Visible Behavior

- Operators can switch between Simplified Chinese (`zh-CN`) and English (`en-US`) from the shell footer.
- The selected locale persists across page reloads for the same browser profile.
- Runtime-generated labels/messages that depend on `window.G3KUI18n.t(...)` update to the active locale without requiring backend restart.

## CEO Composer Runtime

The Leader/CEO composer now has two distinct runtime behaviors that maintainers need to keep straight.

### 1. Active-Turn Button Semantics

- If the current session is idle and the composer is empty, the primary button stays in a disabled `send` state.
- If a user-visible turn is currently running and the composer is empty, the primary button switches to `pause`.
- If the composer contains text or attachments, the primary button switches back to `send` even when a user turn or heartbeat turn is still running.

This is intentional. The composer button no longer means "pause whenever a turn is active"; it means "pause only when the user has not prepared a follow-up payload".

### 2. Queued Follow-Ups

- Browser-side queued follow-ups are stored per session and rendered above the composer.
- Sending while a turn is active enqueues a follow-up instead of interrupting the current turn.
- The queue is consumed automatically in FIFO order after the current turn finishes.
- Consumption is sequential across multiple turns: each queued item becomes the next outbound user message, and the queue keeps draining until it is empty or the session re-enters a blocked state.

### Manual Pause Resume Rule

- If the session is in a resumable manual-pause state, the next outbound user message must resume the paused request with additional context.
- It must not be treated as a completely new independent prompt.
- This distinction matters because otherwise the model only sees the fragmentary follow-up text, such as "前10个", and loses the original task intent.

### Heartbeat Compatibility

- Heartbeat turns count as active session work for the composer button and queueing logic.
- Queued follow-ups should not interrupt heartbeat execution.
- Once heartbeat finishes and the session becomes dispatchable again, queued follow-ups may begin draining automatically.

### Heartbeat/Cron Visibility Versus Prompt Inheritance

- Browser-side CEO timeline rendering and inflight bubbles are allowed to show heartbeat / cron 的原始处理流程。
- This is sourced from session/inflight snapshots, not from the next real user turn's near-field prompt history.
- Maintainers should not assume "frontend can see it" means "the next prompt inherits it verbatim".

The current rule is:

- UI may show heartbeat / cron stage openings, tool calls, execution trace, and compression state directly.
- The next real user turn still filters internal-only history from its local raw history injection.
- To avoid forgetting that work, heartbeat / cron agent-side raw execution context is expected to flow into the global semantic summary path, so later user turns can recover the important meaning without replaying the entire internal turn transcript.

## Verification Pointers

Use these focused checks when validating i18n shell behavior:

- `python -m pytest tests/web/test_frontend_i18n.py -v`
- `python -m pytest tests/resources/test_bootstrap_runtime_status.py -v`

## CEO Compression UI Contract

The browser shell still receives `compression` snapshot data, but it now refers only to semantic-summary refresh state.

- The UI should not assume there is any older message-count compaction stage behind this field.
- `compression.status` now reflects semantic-summary lifecycle only.
- The frontend may still display heartbeat / cron live execution and compression activity from snapshots, but later real user turns depend on the semantic summary path rather than any hidden legacy history compactor.
