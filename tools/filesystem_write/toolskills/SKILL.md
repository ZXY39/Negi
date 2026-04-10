# filesystem_write

Use this to create or replace a file with full content.

Provide:
- `path`: absolute file path
- `content`: full file body

For temporary scratch files, prefer `runtime_environment.task_temp_dir` when available, otherwise workspace `temp/`.
