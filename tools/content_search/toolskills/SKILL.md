# content_search

Use this to search one content target for a specific string or pattern.

Provide:
- `query`: required search string
- `ref`: an `artifact:` content ref when you already have one. Refs are system-assigned — reuse one exactly as given by a task event, task/node detail, or a prior content result; never guess or reformat an artifact id.
- `path`: an absolute file path when you need path mode
- `view`: optional `canonical` or `raw`; prefer `canonical`
- `limit`, `before`, `after`: optional search window controls

If both `ref` and `path` are provided, the tool attempts both targets and returns separate `ref` and `path` results.

Search first, then open only the relevant excerpt instead of requesting the full body.

## Binary targets (PDF, images, xlsx, archives)

For a binary target the search runs over **raw bytes** (its text view is only a placeholder, so a text
search would always miss). The result then carries:

- `byte_level: true`, `binary: true`
- `size_bytes` — real on-disk size
- `line_count` / `char_count` are `0` (line semantics do not apply)
- hits: `byte_offset` (0-based byte position) + `preview` (context bytes, non-printables shown as `.`)

Byte-level search is how you verify binary structure cheaply — e.g. `query: "%PDF"` returns a hit at
`byte_offset: 0` for a real PDF. Absence of a hit is meaningful only with `byte_level: true`.
