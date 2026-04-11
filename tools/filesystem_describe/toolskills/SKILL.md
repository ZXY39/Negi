# filesystem_describe

Use this to inspect one absolute file path before reading or mutating it.

Provide:
- `path`: absolute file path

Use it when you need metadata like file size, line count, type, or content summary.

Directories are not supported here. Use `filesystem_list` for directory contents and `filesystem_search` for directory subtree search.

Do not pass `artifact:` refs here; use the `content` tool for those.
