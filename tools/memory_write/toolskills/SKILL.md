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

## Parameter Shape

- Pass a top-level `items` array.
- Each `items[*]` entry must include `kind`, `key`, `value`, `statement`, and `source_excerpt`.
- `kind` must be one of: `profile`, `preference`, `constraint`, `default`, `avoidance`, `workflow`, `project_fact`, `other`.

## Example

```json
{
  "items": [
    {
      "kind": "workflow",
      "key": "task_time_precision",
      "value": "write explicit timestamps in task descriptions",
      "statement": "When creating tasks, write explicit timestamps in task descriptions.",
      "source_excerpt": "记住要写清楚具体时间"
    }
  ]
}
```
