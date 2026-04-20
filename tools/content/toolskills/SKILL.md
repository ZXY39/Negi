# content

Legacy compatibility wrapper for content navigation.

Prefer the split tools for new calls:
- `content_describe`
- `content_search`
- `content_open`

Use `content` only when an older flow still expects the combined `action=...` interface.

Rules:
- Do not request the full body first. Describe or search, then open only the relevant excerpt.
- For `artifact:` refs, prefer `ref` mode. Do not pass content refs to `filesystem`.
- `path` mode accepts absolute paths only.
- For `action=search` and `action=open`, if both `ref` and `path` are provided, the wrapper attempts both targets and returns separate per-target results.
- For `action=open`, treat `start_line` / `end_line` and `around_line` / `window` as mutually exclusive selector families.
- For `action=open`, line and window values are 1-based integers, and `window` requires `around_line`.
- When `restrict_to_workspace` is enabled, `path` must stay inside the allowed workspace.
- Prefer `view=canonical` for wrapped refs. Use `view=raw` only when debugging wrapper payloads.
