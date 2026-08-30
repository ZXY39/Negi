# manage_task_nodes

Use this CEO-only task runtime tool after a node is paused.

- `resume`: continue an actually paused node from its persisted runtime frame.
- `keep_paused`: keep the node paused and include a concrete reason in `remark`.
- `fail`: mark an actually paused node failed so its parent pipeline can continue.
- `pause`: request an agent pause for a running or queued node; it takes effect at the next safe boundary.

Actions are checked independently per node, so a conflict on one node does not block valid actions for the rest of the batch.
