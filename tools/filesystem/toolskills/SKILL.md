# filesystem

Legacy compatibility wrapper for the original multi-action filesystem tool.

Prefer the narrow tools for new calls:
- `filesystem_describe`
- `filesystem_search`
- `filesystem_open`
- `filesystem_list`
- `filesystem_write`
- `filesystem_edit`
- `filesystem_delete`
- `filesystem_propose_patch`

Use this legacy wrapper only when an older prompt or runtime already expects the single `action` contract, or when `head` / `tail` are specifically needed.

Rules that still apply:
- `path` must be absolute.
- Use the `content` tool for `artifact:` refs instead of passing them as filesystem paths.
- Temporary scratch files should live under `runtime_environment.task_temp_dir` when available, otherwise under workspace `temp/`.
- Third-party tool payloads belong under `externaltools/`, not under `tools/`.
