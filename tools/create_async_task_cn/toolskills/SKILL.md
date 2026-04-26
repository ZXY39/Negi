# create_async_task

Create a detached background task in the main runtime.

## When To Use
- The work is too broad, slow, or multi-step to finish inline in the current CEO turn.
- You need a stable `core_requirement` for the whole task tree.
- You want downstream nodes to continue working asynchronously.

## Required Parameters
- `task`: the full downstream task prompt.
- `core_requirement`: one-sentence distilled core requirement. It must not simply copy `task`.
- `execution_policy`: must include `mode`.
  - `focus`: do only the highest-value, most directly relevant work.
  - `coverage`: still prioritize the highest-value work first, but allow broader completion when needed.

## Optional Parameters
- `file_targets`: optional list of exact file reopen targets for downstream work.
  - Each item should include exact `path` and/or exact `ref`.
  - Use this when the task depends on specific uploaded files, local files, or artifacts.
  - Use `null` or `[]` when the task does not depend on specific files.
- `requires_final_acceptance=true`
- `final_acceptance_prompt`: clear acceptance criteria for the final result.

## Task Prompt Requirements
- Describe the goal, scope, important clues, and expected output.
- If downstream nodes should consult certain skills or tool context first, say that explicitly.
- If the task depends on uploaded files or artifacts, mention in `task` which files matter, and put the exact reopen targets into `file_targets`.
- Do not use placeholders like `user_uploads`, `current_uploads`, or `user_image_and_docx` instead of real `path` / `ref`.
- Do not paste whole files or large tool outputs into the task prompt; provide paths, refs, search targets, or line ranges instead.

## Notes
- `core_requirement` must not be empty.
- `execution_policy.mode` must be explicit.
- When `requires_final_acceptance=true`, `final_acceptance_prompt` is required.
