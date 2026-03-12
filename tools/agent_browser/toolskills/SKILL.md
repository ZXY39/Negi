# agent_browser

Browser automation powered by the external agent-browser CLI. Use this tool when the user asks to open sites, search, click, fill forms, log in, read page text, take screenshots, or download files. After opening a page, use snapshot before interacting, and re-run snapshot after navigation or DOM changes. When the user explicitly asks to open a visible browser or watch browser actions, call this tool with headless=false. Use headless=true only for background probing or silent browser tasks.

## Parameters
- `command`: Primary agent-browser command, e.g. open, snapshot, click, fill, get, wait, cookies, storage, screenshot, state, or close.
- `args`: Positional arguments for the command. Example: command=get, args=[text, @e1].
- `session`: Optional browser session override. Defaults to the current Nano session.
- `headless`: Whether to run in background mode. Use false when the user wants to see the browser window.
- `timeout_s`: Optional command timeout in seconds.

## Usage
Use `agent_browser` only when it is the most direct way to complete the task.
