# manage_task_nodes

Use this CEO-only task runtime tool to control task nodes: resume, fail, or keep
paused nodes, and pause running ones. It is the entry point for acting on
`task_node_error` (error-pause) decisions.

Actions:

- `resume`: continue an actually paused node from its persisted runtime frame.
- `keep_paused`: keep the node paused and include a concrete reason in `remark`.
- `fail`: mark an actually paused node failed so its parent pipeline can continue.
- `pause`: request an agent pause for a running or queued node; it takes effect at the next safe boundary.

`remark` is one batch-wide annotation shared by all entries of the call: the
failure reason for `fail`, the registration remark for `keep_paused` (required
there), the annotation for `pause`. Per-entry remarks are not supported — split
into separate calls if different reasons matter.

## Two call shapes (mutually exclusive)

1. Single-action batch: `node_ids` + one `action` for all of them, optional
   `cascade`. Without cascade, nodes are checked independently — a conflict on
   one node does not block valid actions for the rest. With `cascade=true` the
   call becomes an atomic validated batch (see below).
2. `targets`: a list of `{node_id, action, cascade?}` entries — one atomic call
   that can mix actions (e.g. pause subtree a while failing subtree b). Entries
   omitting `cascade` inherit the top-level `cascade` flag.

## Cascade (propagate down)

With `cascade=true` the action applies to the whole subtree rooted at the node.
Passing the task root node with `cascade=true` targets the entire tree. The
subtree membership is a snapshot taken at call time: nodes spawned afterwards are
not covered (a paused ancestor defers their dispatch anyway).

Cascade/targets calls validate the whole batch **before** applying anything.
If validation fails, the entire call is rejected and no node changes. Error codes
(returned as `{"ok": false, "error": ...}`):

| error | meaning | how to fix |
| --- | --- | --- |
| `subtree_overlap` | a node is covered by 2+ targets' subtrees (e.g. an ancestor and its descendant in one batch); `conflicts` lists each covered node id and the claiming targets | every node must belong to exactly one entry: drop the inner entry when the ancestor's subtree already covers the intent, or split into two ordered calls |
| `node_not_found` | a target node id does not exist in the task | re-check the ids (e.g. via `task_progress`) |
| `node_terminal` | a target root is already success/failed | nothing to do — the node is finished |
| `node_already_paused` | a non-cascade `pause` target is already paused (cascade pause tolerates an already-paused root: the root is skipped and descendants still receive the pause request) | use cascade, or drop the entry |
| `node_not_paused` | a `resume`/`keep_paused`/`fail` target root is not paused | pause it first, or target the node that is actually paused |
| `subtree_not_fully_paused` | a cascade `fail` subtree contains non-terminal descendants that are not paused; `blocking_node_ids` lists them | run the two-step flow below |

Within an accepted batch, individual descendants whose state conflicts (terminal,
already paused, not paused) are skipped per node and reported in `items`; they do
not reject the batch.

Cascade `keep_paused` only re-annotates members that are **already paused** (root
plus paused descendants); it never pauses running descendants — use cascade
`pause` for that.

## Failing a whole subtree

Two-step flow, both calls with `cascade=true` on the subtree root:

1. `pause` — flags every non-terminal member immediately; running descendants
   drain at their next safe boundary.
2. `fail` — issue it right after step 1: the gate accepts pause-requested
   (still draining) nodes, so no waiting or polling is needed.

Never fail a subtree whose descendants carry no pause flag — that batch is
rejected with `subtree_not_fully_paused`.

Warnings:

- Failing the task **root** node (with or without cascade) terminates the whole task.
- Cascade `resume` clears pause state for the entire subtree, including
  error-pause registrations and their heartbeat retry tracking for descendants.
  Cascade `pause` never overwrites an already-paused descendant (its original
  `pause_reason=error` registration is preserved).
