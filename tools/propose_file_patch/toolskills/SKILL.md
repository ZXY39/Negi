# propose_file_patch

Create a reviewable patch artifact instead of editing the target file in place.

## When To Use
- Use `propose_file_patch` when a change should stay reviewable before apply.
- Prefer it over `write_file` or `edit_file` when governance, approval, or human review matters.
- Do not use it for broad refactors when `old_text` would match multiple locations.

## Parameters
- `path`: Target file path.
- `old_text`: Exact existing text to replace. It must appear exactly once.
- `new_text`: Replacement text.
- `summary`: Optional short summary for the generated patch artifact.

## Notes
- The tool does not modify the file directly.
- The output includes a patch artifact and a diff preview.
- If `old_text` is missing or ambiguous, tighten the selection before retrying.
