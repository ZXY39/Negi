# filesystem_describe

Use this to inspect one absolute filesystem path before reading or mutating it.

Provide:
- `path`: absolute file or directory path

Use it when you need metadata like file size, line count, type, or content summary. Do not pass `artifact:` refs here; use the `content` tool for those.
