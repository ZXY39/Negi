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
