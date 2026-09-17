# content_describe

Use this to inspect one content target before searching or opening it.

Provide:
- `ref`: an `artifact:` content ref when you already have one. Refs are system-assigned — reuse one exactly as given by a task event, task/node detail, or a prior content result; never guess or reformat an artifact id.
- `path`: an absolute file path when you need path mode
- `view`: optional `canonical` or `raw`; prefer `canonical`

Use it to get metadata, line counts, wrapper resolution details, and summary information without pulling the full body into context.

## Binary targets (PDF, images, xlsx, archives)

A binary target has no text body to show. Its result carries, instead of file content:

- `binary: true` / `content_display_replaced: true`
- `size_bytes` — the file's **real on-disk size**
- `line_count` / `char_count` — statistics of a **placeholder string** (`[二进制文件：name]`), not of the file

Never read `line_count` / `char_count` / the placeholder preview as the file's size, type, or validity —
a 600KB valid PDF and a 34-byte stub both show `(1 lines, 22 chars)`. For binary targets `size_bytes`
is the only size fact; verify structure with `content_search` (byte-level for such targets).

Directories are not supported in path mode. Use `exec` for local directory exploration and subtree searches, and follow the current runtime tool contract for its active execution mode.
