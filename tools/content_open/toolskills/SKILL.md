# content_open

Use this to read one local excerpt from a content target.

Provide:
- `ref`: an `artifact:` content ref when you already have one
- `path`: an absolute file path when you need path mode
- `view`: optional `canonical` or `raw`; prefer `canonical`
- one range selector: `start_line` and `end_line`, or `around_line` with `window`
- line and window values are 1-based integers
- do not mix `start_line` / `end_line` with `around_line` / `window`
- do not pass `window` without `around_line`

If both `ref` and `path` are provided, the tool attempts both targets and returns separate `ref` and `path` results.

Open only the lines you need. If you do not know where to look yet, use `content_describe` or `content_search` first.
