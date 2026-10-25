# send_message

Frontdoor-to-node messaging inside tasks owned by this session.

## Choosing targets

- A message that changes the task itself — new requirements, dropped scope, a corrected direction, a deadline — goes to **the root node only**. The root owns how the task tree carries it down.
- Address one specific node only when the message is about that node alone: it answers that node's question, corrects that node's own output, or explicitly names it.
- Do not put several nodes in one call to broadcast one text. The body is identical for every target, and a parent never receives a copy of the mail addressed to its children — it relays the text down again, so every child reads it twice and the two copies can drift apart.
- When a change must reach the whole tree, send it to the root once. Add a targeted message to one branch only when that branch needs something the root message does not say.

## Other rules

- Messaging does not control the task: pause / resume / terminate use `manage_task_nodes`.
- Terminal and under-acceptance nodes reject delivery; the result reports the reason per target.
- Put long material in `ref` and keep `content` a summary.
