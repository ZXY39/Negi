# filesystem_edit

Replace one region in one existing file with `new_text`. `path` must be absolute.

Two ways to say which region. Both are flat top-level fields; pick one.

**Quote the text** — the default, and self-checking: a wrong guess fails loudly.

```json
{
  "path": "C:\\notes\\todo.md",
  "old_text": "- [ ] call the vendor",
  "new_text": "- [x] call the vendor"
}
```

- `old_text` matches byte-for-byte, indentation and newlines included, and must appear exactly once. Widen it with neighbouring lines until it is unique.
- Read the region with `content_open` first and copy it verbatim.

**Give the line range** — use when the region is long enough that re-typing it costs more than re-counting it.

```json
{
  "path": "C:\\app\\server.py",
  "start_line": 41,
  "end_line": 58,
  "new_text": "def health():\n    return {'ok': True}\n"
}
```

- 1-based, inclusive of both ends, counted from a read of the file as it is now.
- A range cannot mismatch loudly: a stale or off-by-one range replaces the right number of the **wrong** lines and still reports success. The result echoes `replaced lines 41-58, first was: ...` — check that quoted line, and if it is not what you meant, re-read and redo the edit.
- Do not send several range edits for the same file in one turn: each one shifts the lines the others were counted against. Send one range per call and re-read in between, or quote `old_text`, which is immune to shifting.

`new_text: ""` deletes the region. To change several regions, make one call per region.

Repeating an edit is safe: when the region already holds `new_text` — `old_text` absent while `new_text` is present once, or the given lines already equal `new_text` — the result starts with `Already applied:` and the file is left untouched. Treat that as success instead of re-reading and retrying.

When the whole file is being created or replaced, use `filesystem_write`.
