# filesystem_open

Use this to read a bounded excerpt from a local file.

Provide:
- `path`: absolute file path

Optional:
- `start_line` and `end_line`
- `around_line` and `window`

Prefer bounded excerpts over loading large files wholesale.
