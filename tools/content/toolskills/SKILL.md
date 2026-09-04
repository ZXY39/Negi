# content

Legacy compatibility wrapper for content navigation.

Prefer the split tools for new calls:
- `content_describe`
- `content_search`
- `content_open`

Use `content` only when an older flow still expects the combined `action=...` interface.

Rules:
- Do not request the full body first. Describe or search, then open only the relevant excerpt.
- For `artifact:` refs, prefer `ref` mode. Do not pass content refs to `filesystem`. Refs are system-assigned — reuse one exactly as given by a task event, task/node detail, or a prior content result; never guess or reformat an artifact id.
- `path` mode accepts absolute paths only.
- For `action=search` and `action=open`, if both `ref` and `path` are provided, the wrapper attempts both targets and returns separate per-target results.
- For `action=open`, treat `start_line` / `end_line` and `around_line` / `window` as mutually exclusive selector families. Character addressing `start_char` / `end_char` is a third family, mutually exclusive with both line families.
- For `action=open`, line and window values are 1-based integers, and `window` requires `around_line`. `start_char` / `end_char` are **1-based Unicode code-point** offsets (not byte, not 0-based) with `end_char >= start_char`; 0-based code point `O` maps to `start_char = O + 1`. Prefer line addressing for ordinary multi-line source code (`grep -n` gives line numbers); use character addressing only for single-line or oversized content.
- For `action=open`, the per-call excerpt cap is 16000 chars (line mode) or 128000 chars (character mode). Results are line-aligned (never cut mid-line); a single-line oversized target needs `start_char`/`end_char` to paginate, and MB-scale single-line files are better handled by `exec` targeted extraction. The 128000-char cap costs ~64K–85K tokens for Chinese text — use it sparingly.
- When `restrict_to_workspace` is enabled, `path` must stay inside the allowed workspace.
- Prefer `view=canonical` for wrapped refs. Use `view=raw` only when debugging wrapper payloads.
