# filesystem_edit

Use this for precise edits to an existing file.

Preferred:
- Set `mode="text_replace"` for text replacement calls.
- Set `mode="line_range"` for line-based replacement calls.

Text-replace mode:
- `path`
- `mode="text_replace"` (recommended)
- `old_text`
- `new_text`

Line-range mode:
- `path`
- `mode="line_range"` (recommended)
- `start_line`
- `end_line`
- `replacement`

Use exactly one mode per call.

If a caller auto-fills placeholder line-range values such as `start_line=0`, `end_line=0`, and `replacement=""` during a text-replace call, omit them or set `mode="text_replace"`.
