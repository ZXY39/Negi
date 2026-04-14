# filesystem_copy

Use this when the task must duplicate local files or directory trees without removing the original source.

Provide:
- `operations`: array of `{source, destination}` absolute-path pairs
- `overwrite`: optional, only applies to existing destination files
- `create_parents`: optional, creates destination parent folders when missing
- `continue_on_error`: optional, continue after a failed item

Rules:
- `source` and `destination` must both be absolute paths.
- Directory sources may only copy into destinations that do not already exist.
- Existing destination directories are rejected even when `overwrite=true`.
