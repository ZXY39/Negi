# memory_note

Load the detailed body of a note whose token is already visible in the memory block.

## Must Use

- Before you state a long-term rule, workflow or spec that a memory row points at. A memory entry shows a `note:` line, or a `归并记忆` line shows `(note: ...)`, and that topic is what the current turn needs.
- The row is the entry only: steps, thresholds and interfaces live in the note body.

## Input

- `ref`: pass the handle printed after the `note:` label, not the label itself. A real note file name also resolves, with or without `.md`.

## Do Not Use

- Guessing a note token that is not visible in the current memory snapshot.
- Writing, revising, or forgetting memory (that is `memory_update`).
