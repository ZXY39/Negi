# memory_write

Queue a durable long-term memory write request.

## Must Use

- The user explicitly asks the system to remember a stable rule, preference, default, identity detail, or project fact.
- The current turn establishes a durable memory the user expects to persist across later sessions.

## Input

- Pass a single `content` string containing the raw memory candidate.

## Important Rules

- Do not send temporary runtime state, processing markers, pause/resume control text, or speculative guesses.
- The memory agent will decide the final compact `MEMORY.md` wording and whether a detailed note file is needed.
