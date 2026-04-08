# memory_delete

## When To Use

Use `memory_delete` only when you are deleting a specific structured fact and you have an exact identifier:
- `fact_id` from memory runtime outputs
- `canonical_key` from structured fact payloads

Do not use this tool for bulk deletion, fuzzy matching, or guessing. Prefer `memory_search` first to locate the exact target.

## Parameters

- `fact_ids`: list of fact ids to delete (exact match).
- `canonical_keys`: list of canonical keys to delete (exact match).

## Returns

Returns JSON including `ok` and `deleted` counts.

