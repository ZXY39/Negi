# content_describe

Use this to inspect one content target before searching or opening it.

Provide:
- `ref`: an `artifact:` content ref when you already have one
- `path`: an absolute file path when you need path mode
- `view`: optional `canonical` or `raw`; prefer `canonical`

Use it to get metadata, line counts, wrapper resolution details, and summary information without pulling the full body into context.

Directories are not supported in path mode. Use `exec` for local directory exploration and subtree searches, and follow the current runtime tool contract for its active execution mode.
