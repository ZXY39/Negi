# content_open

Use this to read one local excerpt from a content target.

Provide:
- `ref`: an `artifact:` content ref when you already have one. Refs are system-assigned — reuse one exactly as given by a task event, task/node detail, or a prior content result; never guess or reformat an artifact id.
- `path`: an absolute file path when you need path mode
- `view`: optional `canonical` or `raw`; prefer `canonical`
- Line addressing: `start_line` / `end_line` (1-based integers) for the line range to open
- Character addressing: `start_char` / `end_char` (1-based integers) — mutually exclusive with all line selectors

If both `ref` and `path` are provided, the tool attempts both targets and returns separate `ref` and `path` results.

## How much each call returns

- **No range params** → reads from line 1, returning whole lines up to the line-mode cap of **16000 chars**. It never cuts a line in half, so a small file may be returned in full even if that is more or fewer than a fixed line count.
- **Line range** (`start_line`/`end_line` or `around_line`/`window`) → same 16000-char line-aligned cap within the range.
- **Character range** (`start_char`/`end_char`) → slices by raw character offset with a higher cap of **128000 chars**. This is the way to paginate single-line or oversized content that line ranges cannot reach.

If the excerpt hits the cap, the result sets `truncated: true` and appends a notice reporting how many chars/lines were shown vs. remain and how to continue (`start_line=…`, `start_char=…`, or exec). Follow that notice.

## Character offset unit (1-based code point, not byte)

`start_char` / `end_char` are **1-based Unicode code-point** offsets into the text (each Python `str` character = 1). They are NOT byte offsets and NOT 0-based.

- 0-based code-point offset `O` → `start_char = O + 1`
- A byte offset (e.g. from `grep -b`) equals the code-point offset only for ASCII; with UTF-8 non-ASCII (CJK, emoji), one character is multiple bytes, so byte offsets do not map directly — convert to code points first.
- For ordinary multi-line source code, prefer line addressing (`start_line`/`end_line`) — `grep -n` gives line numbers directly. Character addressing is a fallback for single-line or oversized content where line ranges cannot paginate.

## Single-line / oversized content

For a target that is one very long line (minified JSON, single-line logs, base64), line params are useless. Use `start_char`/`end_char` to paginate by characters (e.g. `start_char=1&end_char=128000`, then continue from the returned `end_char`). For **MB-scale single-line files**, prefer `exec` targeted extraction (`jq`/`grep`/python) instead of paginating — you almost never need the raw MB, just a field or aggregate.

⚠️ **Token cost**: the 128000-char char-mode cap is ~32K tokens for Latin text but ~64K–85K tokens for Chinese. Only use the full char cap when you genuinely need that much; otherwise request a smaller window.

Open only what you need. If you do not know where to look yet, use `content_describe` (sizes/previews) or `content_search` (find the line) first.

If the target is a historical image path or image ref and you need direct visual inspection, call `content_open`.

On multimodal routes, the opened image is attached to the next model request only.
On non-multimodal routes, image open fails with `非多模态模型无法打开图片`.
