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
- When `restrict_to_workspace` is enabled, `path` must stay inside the allowed workspace.
- Prefer `view=canonical` for wrapped refs. Use `view=raw` only when debugging wrapper payloads.
