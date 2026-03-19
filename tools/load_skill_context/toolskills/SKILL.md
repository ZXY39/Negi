# load_skill_context

Load layered context for a currently visible skill.

## Parameters
- `skill_id`: The skill id to load.
- `level`: Optional `l0 | l1 | l2`, default `l1`.
- `query`: Optional query for focused `l2` extraction.
- `max_tokens`: Optional token budget.

## Returns
- `content`
- `level`
- `skill_id`
- `excerpt_strategy` / `query` when `l2` is used

## Usage
Use `load_skill_context` only when it is the most direct way to complete the task.
