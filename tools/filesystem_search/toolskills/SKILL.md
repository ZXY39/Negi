# filesystem_search

Use this to search one file or one directory subtree with an absolute `path`.

Provide:
- `path`: absolute file or directory path
- `query`: search term or regex

Optional:
- `limit`
- `before`
- `after`

If the result says `requires_refine=true` or `overflow=true`, narrow the path or make the query more specific before retrying.
