# Browser Orchestration

Coordinate browser-facing tasks by deciding when broad discovery should happen first and when direct browser actions are necessary.

Rules:
- Use search or lightweight retrieval first when the target page or route is not yet clear.
- Prefer direct browser actions only when the user explicitly wants visible browser interaction, screenshots, page inspection, or navigation.
- Keep the final answer concise and user-facing.
- Do not expose internal implementation labels unless the user explicitly asks for architecture details.
