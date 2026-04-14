# filesystem_delete

Use this only when the target really should be removed from disk.

Provide:
- `paths`: array of absolute file or directory paths
- `recursive`: required for directory deletion
- `allow_missing`: optional, treat already-missing targets as successful
- `continue_on_error`: optional, continue after a failed item

This is destructive. Double-check every path first, especially when deleting generated artifacts versus source files.
