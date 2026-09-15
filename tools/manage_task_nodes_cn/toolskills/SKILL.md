# manage_task_nodes

Use this CEO-only task runtime tool after a node is paused.

Actions:

- `resume`: continue an actually paused node from its persisted runtime frame.
- `keep_paused`: keep the node paused and include a concrete reason in `remark`.
- `fail`: mark an actually paused node failed so its parent pipeline can continue.
- `pause`: request an agent pause for a running or queued node; it takes effect at the next safe boundary.

## Two call shapes (mutually exclusive)

1. Legacy batch: `node_ids` + one `action` for all of them, optional `cascade`.
   Actions are checked independently per node, so a conflict on one node does not
   block valid actions for the rest of the batch.
2. `targets`: a list of `{node_id, action, cascade?}` entries, one atomic call that
   can mix actions (e.g. pause subtree a while failing subtree b).

## Cascade (propagate down)

With `cascade=true` the action applies to the whole subtree rooted at the node.
Passing the task root node with `cascade=true` targets the entire tree. The
subtree membership is a snapshot taken at call time: nodes spawned afterwards are
not covered (a paused ancestor defers their dispatch anyway).

Cascade/targets calls validate the whole batch **before** applying anything.
If validation fails, the entire call is rejected and no node changes. Error codes
(returned as `{"ok": false, "error": ...}`):

| error | meaning |
| --- | --- |
| `subtree_overlap` | a node is covered by 2+ targets' subtrees (e.g. an ancestor and its descendant in one batch); `conflicts` lists each covered node id and the claiming targets |
| `node_not_found` | a target node id does not exist in the task |
| `node_terminal` | a target root is already success/failed |
| `node_already_paused` | a non-cascade `pause` target root is already paused (cascade pause tolerates an already-paused root: the root is skipped and descendants still receive the pause request) |
| `node_not_paused` | a `resume`/`keep_paused`/`fail` target root is not paused |
| `subtree_not_fully_paused` | a cascade `fail` subtree contains non-terminal descendants that are not paused; `blocking_node_ids` lists them |

Within an accepted batch, individual descendants whose state conflicts (terminal,
already paused, not paused) are skipped per node and reported in `items`; they do
not reject the batch.

## Failing a whole subtree

Cascade `fail` is a two-step flow: first `pause` with `cascade=true` (running
descendants stop at their next safe boundary), then `fail` with `cascade=true`.
Never fail a subtree with running descendants directly — the batch is rejected.

Warnings:

- Failing the task **root** node (with or without cascade) terminates the whole task.
- Cascade `resume` clears pause state for the entire subtree, including
  error-pause registrations and their heartbeat retry tracking for descendants.
  Cascade `pause` never overwrites an already-paused descendant (its original
  `pause_reason=error` registration is preserved).
