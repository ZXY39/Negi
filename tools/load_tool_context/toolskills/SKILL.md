# load_tool_context

Load full context for a currently visible tool or a registered external tool.

## Parameters

- `tool_id`: Tool family id or executor tool name.

## Returns

The payload includes:

- `content` (full raw tool context document)
- `level`
- `l0`
- `l1`
- `tool_type`
- `install_dir`
- `callable`

Use it when you need installation, update, or usage guidance for an external tool that does not appear in the callable tool list.
