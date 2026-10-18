# load_tool_context

Load full context for a currently visible tool.

## Parameters

- `tool_id`: Tool name as listed in the candidate or callable tool lists.

## Returns

The payload includes:

- `content` (full raw tool context document)
- `level`
- `l0`
- `l1`
- `tool_type`
- `install_dir`
- `callable`

## Usage

- Use it when you need installation, update, troubleshooting, repair, or detailed usage guidance for a tool.
- This is the default entry point for a tool that is visible but not yet callable this turn (a candidate), or is currently unavailable.
- If a callable tool is shown as `【待修复】`, or a tool result reports `repair_required=true` or `tool_state="repair_required"`, call `load_tool_context(tool_id="<tool_id>")` before attempting repair.

## Repair Workflow

For a repair-required tool, the default flow is:

1. Call `load_tool_context(tool_id="<tool_id>")`.
2. Use the recommended repair workflow plus supporting tools such as `repair-tool`, `filesystem`, and `exec` to fix the tool.
3. Refresh or re-check availability.
4. Retry the original tool action.

Only conclude that the tool is completely unusable when progress is blocked by missing credentials, missing approval, or an explicit user stop instruction.
