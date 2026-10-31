# memory_update

Queue a durable long-term memory request in natural language.

## Must Use

- The user explicitly asks the system to remember, revise, or forget a stable rule, preference, default, identity detail, or project fact.
- The current turn establishes a durable memory the user expects to persist across later sessions.

## Input

- Pass a single `content` string holding the raw memory request, for example: `以后回复先给结论` or `忘掉我喜欢简洁回答这条偏好`.

## Important Rules

- Do not send temporary runtime state, processing markers, pause/resume control text, or speculative guesses.
- In a group session, any member's message can justify an update to that group's memory block; it never updates the operator's local long-term memory. Decide by content, not by sender.
- Do not edit `memory/` files yourself; all memory changes go through this tool.
- One call can describe any number of memory changes; do not fan out into batch calls.
- Do not pass memory ids; a memory agent resolves the description against the current snapshot.
- The memory agent will decide the final compact `MEMORY.md` wording and whether a detailed note file is needed.
