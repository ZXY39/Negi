# memory_search

Search long-term memory and return grouped + unified ranked results.

## Must Use

- The answer depends on prior user/project facts not fully present in the current turn.
- You need exact structured-memory identifiers before calling `memory_delete`.

## Structured Memory Output

- Structured memory hits include:
- `fact_id`
- `canonical_key`
- `category`
- `observed_at`
- `expires_at` (when present)

## Deletion Workflow

- Before deleting structured memory, run `memory_search` first.
- Copy `fact_id` and/or `canonical_key` from the matched hit.
- Use those exact identifiers in `memory_delete`.
- Never guess delete targets from paraphrased text.

## Timestamp Guidance

- When reviewing `stateful_fact` hits, treat `observed_at` as the authoritative state timestamp.
- Prefer the most recent active fact for the same canonical key.

## Parameters

- `query`: Concrete lookup query with entities/intent.
- `limit`: Max number of unified results.
- `context_type`: Optional context-type filter.
- `include_l2`: Include L2 preview snippets when available.
- `session`: Optional session key override, e.g. `cli:direct`.
