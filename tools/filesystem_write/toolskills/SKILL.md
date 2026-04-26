# filesystem_write

Use this to create or replace a file with full content.

Provide:
- `path`: absolute file path
- `content`: full file body

Use `filesystem_write` when the whole file should be created or replaced.

If you only need to modify part of an existing file, use `filesystem_edit` instead of rewriting the entire file body.

For temporary scratch files, prefer `runtime_environment.task_temp_dir` when available, otherwise workspace `temp/`.
