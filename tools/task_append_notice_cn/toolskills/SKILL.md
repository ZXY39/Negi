# task_append_notice

Append new requirements, constraints, or acceptance details to an existing unfinished task in the current session.

## When to use
- The work should continue on an existing unfinished task instead of creating a new detached task.
- You need to add a new requirement, constraint, acceptance rule, or follow-up notice to an existing task.
- You need to deliver a requirement to ONE specific node's subtree without freezing the whole task tree (pass `node_ids`).
- A duplicate-precheck or planning step indicates "use append notice instead of create_async_task".

## Parameters
- `message`: Required appended notice text.
- `task_ids`: Optional unfinished task ids in the current session; the notice targets each task's root node (whole-tree distribution).
- `node_ids`: Optional targeted-notice node ids; each becomes a distribution target and only its subtree is frozen during distribution.

## Behavior
- Unified subtree-barrier distribution: the notice is distributed from each target node downward; the frozen scope is the union of the target subtrees (task_ids targets the root, which is the whole tree).
- Target validation: the node must exist, belong to an unfinished task in the current session, be visible in the tree, be an execution node (acceptance nodes are rejected), and be non-terminal (success/failed nodes are rejected — a terminal target's subtree is terminal too).
- Nested targets merge into the topmost ancestor; disjoint targets share one distribution epoch.
- Each target receives the notice by its own state: waiting for children -> a control turn decides which children get it forwarded; being acceptance-inspected -> a decision turn chooses interrupt-inspection-and-rework vs continue-inspection (the inspector is informed either way); actively working -> merged into its context without restart.
- After the subtree distribution completes, the notice content is also propagated upward along the path to the task root: every ancestor (and its live acceptance child, plus the task's final acceptance node) receives an informational copy in its mailbox.
- The task is NOT paused for a targeted notice; a manually paused task or paused target node defers distribution until resumed. The runtime persists the notice and enters task message distribution instead of creating a new task.

## Returns
- Success text such as `已向任务 task:xxx 追加通知。`

## Notes
- This tool is for updating existing unfinished work, not for generic task editing.
- Targets must belong to the current session and must still be unfinished.
- Use `create_async_task` only when the work should become a genuinely new detached task.
