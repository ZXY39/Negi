# process_kill

Stop a background process that **this session/task** started through `exec` with
`background: true`.

## When to use it

- You started a server, watcher, crawl driver, or browser with `exec background: true`
  and the work is done — stop it here so the port is released.
- A background job is stuck or must be restarted. Kill it, then start a new one.

Do **not** use it for anything else: arbitrary pids, processes started outside `exec`,
and the runtime's own processes are not in the ownership table and are rejected.

## Input

- `pid`: the exact number returned by the `exec` result (`"pid": 1234`). Do not guess
  or reuse a pid from an earlier session; pids get recycled by the OS.

## What you get back

- `ok` / `status` (`terminated`, `already_exited`, `identity_mismatch`, `not_owned`)
- `cmd` and `log_path` of the process
- `log_tail`: the last ~2000 characters of its output, so you can explain why it
  exited or failed
- On `not_owned`, the current session/task's owned pids (bounded list) so you can
  pick the right one instead of guessing

## Ownership rules

- A pid belongs to the scope that spawned it: CEO sessions and execution tasks each
  have their own table. A task cannot kill a session's process, and vice versa.
- Identity is checked by process creation time, so a recycled pid is refused rather
  than killed.
- Termination is tree-wide but downward only: children go, ancestors never do.

## Lifecycle

Background processes die with their owner: clearing the session (or finishing the
task's scope) reaps them, and a restart of the runtime reaps entries whose owner
process is gone. You do not need to kill everything "just in case" — but killing what
you no longer need is the cheapest way to free a port.
