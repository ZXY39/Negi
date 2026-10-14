# memory_note

Load the detailed body of a note whose token is already visible in the memory block.

## Must Use

- A memory entry shows a `note:` line, or a `归并记忆` line shows `(note: ...)`, and that topic is what the current turn needs.

## Input

- `ref`: pass the token exactly as it is shown. A real note file name also resolves.

## Do Not Use

- Guessing a note token that is not visible in the current memory snapshot.
- Writing, revising, or forgetting memory (that is `memory_update`).
