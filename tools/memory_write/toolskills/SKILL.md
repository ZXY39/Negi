# memory_write

Write structured long-term memory facts immediately.

## Must Use

- The user explicitly asks the system to remember a durable fact.
- The user states a stable preference, constraint, identity, workflow rule, default, relationship, or project fact that should persist.

## Do Not Use

- Temporary task state or short-lived turn context.
- Guesses, inferred facts, or ambiguous claims.
- Facts not grounded in the current turn's user-provided evidence.

## Timestamp Rules

- Every fact needs an ISO8601 `observed_at` timestamp.
- For `stateful_fact`, `observed_at` is mandatory and must represent when that state was observed or confirmed.
- Prefer explicit user-provided timestamps when available; otherwise use the current turn time in ISO8601 format.
- `expires_at` is optional; set it only when the user clearly gives an expiry boundary.

## Deletion/Correction Workflow

- Do not guess deletion targets.
- If a stored structured fact must be removed or corrected, run `memory_search` first and collect `fact_id` and `canonical_key` from the hit.
- Use those identifiers with `memory_delete`, then write the corrected fact via `memory_write`.

## Parameter Shape

- Pass a top-level `facts` array.
- Each `facts[*]` entry should include: `category`, `scope`, `entity`, `attribute`, `value`, `observed_at`, `time_semantics`, and `source_excerpt`.

## Example

```json
{
  "facts": [
    {
      "category": "stateful_fact",
      "scope": "session",
      "entity": "user",
      "attribute": "task_time_precision",
      "value": "write explicit timestamps in task descriptions",
      "observed_at": "2026-04-08T12:30:00+08:00",
      "time_semantics": "current_state",
      "source_excerpt": "Remember: write explicit timestamps in task descriptions."
    }
  ]
}
```
