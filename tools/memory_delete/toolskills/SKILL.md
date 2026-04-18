# memory_delete

Queue a durable long-term memory delete request using memory text visible in the current `MEMORY.md` snapshot.

## Must Use

- The user explicitly asks to forget a remembered rule, preference, or fact that is already visible in the current memory snapshot.

## Input

- Pass the visible memory text as `target_text`.

## Do Not Use

- Guessing at memories that are not currently visible.
- Bulk deletion.
- Removing memory when correction or replacement would be better.
