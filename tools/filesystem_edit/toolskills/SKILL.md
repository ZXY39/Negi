# filesystem_edit

Use this for precise edits to an existing file.

Preferred workflow:
- Read the file first with `content_open`.
- Use the target-first contract: `path`, `target`, `new_text`.

Preferred target-first locators:
- `target={"by":"exact_text","text":"..."}` when you already have the exact old block and it should match uniquely.
- `target={"by":"anchor_pair","start_anchor":"...","end_anchor":"..."}` when exact old text is too large or too fragile, but stable surrounding anchors exist.
- `target={"by":"line_range","start_line":N,"end_line":M}` only when you already know the exact current lines from a fresh read.

Target-first call shape:
- `path`
- `target`
- `new_text`

Legacy text-replace mode is still supported for backward compatibility:
- `path`
- `mode="text_replace"`
- `old_text`
- `new_text`

Legacy line-range mode is still supported for backward compatibility:
- `path`
- `mode="line_range"`
- `start_line`
- `end_line`
- `replacement`

Do not mix `target` with real legacy edit fields in the same call.

If a caller auto-fills placeholder legacy values such as `start_line=0`, `end_line=0`, `replacement=""`, or empty legacy text fields, omit them. Placeholder cleanup only exists to absorb adapter noise, not to support real mixed-mode edits.
