# filesystem_edit

Replace one exact text region in an existing file.

Read the region first with `content_open`, then quote it verbatim.

Call shape — three top-level fields, all required:

```json
{
  "path": "C:\\notes\\todo.md",
  "old_text": "- [ ] call the vendor",
  "new_text": "- [x] call the vendor"
}
```

Rules:
- `old_text` must match the file byte-for-byte, including indentation and newlines, and must appear exactly once. Widen it with neighbouring lines until it is unique.
- Keep `old_text` as small as it can be while still unique. Do not quote a whole block to change one line.
- Pass `new_text: ""` to delete the region.
- `old_text` and `new_text` sit side by side at the top level. Neither belongs inside an object.

Repeated calls are safe: when `old_text` is absent because the change is already on disk, the result starts with `Already applied:` and the file is left untouched. Treat that as success instead of re-reading and retrying.

One call changes one region; to change several regions, make one call per region. When the whole file is being created or replaced, use `filesystem_write`.
