# memory_delete

Queue a durable long-term memory delete request using memory ids visible in the current `MEMORY.md` snapshot.

## Must Use

- The user explicitly asks to forget a remembered rule, preference, or fact that is already visible in the current memory snapshot.

## Input

- Pass one visible memory id as `id`.
- Or pass multiple visible memory ids as `ids=[...]`.

## Do Not Use

- Guessing at memories that are not currently visible.
- Removing memory when correction or replacement would be better.
