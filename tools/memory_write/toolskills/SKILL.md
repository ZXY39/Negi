# memory_write

Write explicit long-term memory immediately.

## Must Use

- The user explicitly asks the system to remember something durable.
- The user establishes a stable preference, constraint, default, avoidance rule, workflow rule, or durable project fact.

## Do Not Use

- Temporary task state.
- Guesses or inferred facts.
- Short-lived conversation context.
- Unconfirmed or ambiguous claims.

## Write Style

- Normalize each item into a stable `key`.
- Use a reusable `statement` that can be retrieved later.
- Use `source_excerpt` from the current user turn only.
