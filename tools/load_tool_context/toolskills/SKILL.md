# load_tool_context

Load the layered guide for a currently visible tool or a registered external tool.

## Parameters

- `tool_id`: Tool family id or executor tool name.
- `level`: Optional `l0 | l1 | l2`, default `l1`.
- `query`: Optional query for focused `l2` extraction.
- `max_tokens`: Optional token budget.

## Returns

The payload includes:

- `content`
- `tool_type`
- `install_dir`
- `callable`

Use it when you need installation, update, or usage guidance for an external tool that does not appear in the callable tool list.
