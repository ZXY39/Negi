# filesystem_stat

Read-only measurement of files and directories. Use it to verify a delivered set of artifacts: how many
files exist, their real on-disk size, when they were last written, and their **real file names**.
It never writes, deletes or modifies anything.

Provide:
- `paths`: array of absolute file or directory paths (required)
- `max_entries`: optional cap on entries listed per directory (default 200, max 2000). Aggregates
  (`file_count` / `total_bytes` / `largest_file` / `smallest_file`) always cover the whole subtree.

Returns per path:
- file → `exists`, `kind: "file"`, `size_bytes` (real on-disk size), `mtime`, `suffix`
- directory → `kind: "directory"`, `file_count`, `dir_count`, `total_bytes`, `largest_file`, `smallest_file`,
  plus `entries` (name / kind / size_bytes / mtime for direct children)
- missing → `exists: false` (no size/mtime fields)

## Why this exists for verification

- `size_bytes` here is the file's **real byte count**. Content tools (`content_describe` / `content_open`)
  show a placeholder for binary targets, and their `line_count` / `char_count` describe that placeholder,
  not the file — never read those as the file's volume.
- Directory listings are the authoritative source of file names. Never construct a sample name from
  "category + index"; take samples from a listing or from the delivery manifest (final CSV / report).
- `mtime` distinguishes artifacts written in this round from stale ones.

## Boundaries

- Read-only by construction: creating, editing, moving and deleting live in separate role-gated tools
  (`filesystem_write` / `filesystem_edit` / `filesystem_copy` / `filesystem_move` / `filesystem_delete`).
- Aggregates stop at 200k files / depth 8 and set `scan_truncated: true` when they do.
- Paths outside the workspace are measured unless the tool is configured with `restrict_to_workspace: true`.
