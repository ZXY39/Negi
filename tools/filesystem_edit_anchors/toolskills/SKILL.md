# filesystem_edit_anchors

Replace everything between two anchors in an existing file, inclusive of both anchors.

Use this when the region to replace is too large or too volatile to quote exactly, but you know two short strings that bound it. Read the file first with `content_open` so the anchors are copied verbatim from the current content.

Call shape — four top-level fields, all required:

```json
{
  "path": "C:\\app\\server.py",
  "start_anchor": "def health_check():",
  "end_anchor": "    return {'ok': True}",
  "new_text": "def health_check():\n    return {'ok': True, 'version': 2}"
}
```

Rules:
- Each anchor must appear exactly once in the file, with `end_anchor` occurring after `start_anchor`.
- The matched region runs from the start of `start_anchor` to the end of `end_anchor`, so both anchors must be reproduced inside `new_text` if they should survive the edit.
- Keep anchors short and stable. A one-line signature or a closing brace is enough; quoting the body defeats the purpose.

If you know the exact text to replace, use `filesystem_edit` instead — it needs no anchors.

Repeated calls are safe: when the anchored region already holds `new_text`, the result starts with `Already applied:` and the file is left untouched.
