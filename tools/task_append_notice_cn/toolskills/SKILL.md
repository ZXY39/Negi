# task_append_notice

Append new requirements, constraints, or acceptance details to an existing unfinished task in the current session.

## When to use
- The work should continue on an existing unfinished task instead of creating a new detached task.
- You need to add a new requirement, constraint, acceptance rule, or follow-up notice to an existing task.
- A duplicate-precheck or planning step indicates "use append notice instead of create_async_task".

## Parameters
- `message`: Required appended notice text.
- `task_ids`: Optional unfinished task ids in the current session.
- `node_ids`: Optional live-tree node ids that resolve back to unfinished tasks in the current session.

## Behavior
- Task ids are used directly.
- Node ids resolve back to their owning unfinished task root before delivery.
- The runtime persists the notice and enters task message distribution instead of creating a new task.

## Returns
- Success text such as `已向任务 task:xxx 追加通知。`

## Notes
- This tool is for updating existing unfinished work, not for generic task editing.
- Targets must belong to the current session and must still be unfinished.
- Use `create_async_task` only when the work should become a genuinely new detached task.
