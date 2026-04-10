# filesystem_propose_patch

Use this when you want a patch artifact instead of a direct file mutation.

Provide:
- `path`: absolute file path
- `old_text`
- `new_text`

Optional:
- `summary`

This keeps the working file unchanged and returns a patch artifact for review or later application.
