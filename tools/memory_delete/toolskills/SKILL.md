# memory_delete

Queue a durable long-term memory delete request using a natural-language description of the memory to forget.

## Must Use

- The user explicitly asks to forget a remembered rule, preference, or fact that is already visible in the current memory snapshot.

## Input

- Pass the remembered content as `content`.
- Describe the memory naturally, for example: `忘掉我喜欢简洁回答这条偏好`.

## Do Not Use

- Guessing at memories that are not currently visible.
- Removing memory when correction or replacement would be better.
