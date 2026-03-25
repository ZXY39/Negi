# Heartbeat Rules

These rules apply to heartbeat-driven internal turns.

1. If the heartbeat event is a task terminal result, you must notify the current user.
2. A task terminal result means the task has reached a final status such as `success` or `failed`.
3. For task terminal results, do not reply with `HEARTBEAT_OK`.
4. The user-facing notification should be concise and direct, and should include:
   - the task title or task ID when helpful
   - whether the task completed successfully or failed
   - a short result summary or failure reason when available
5. Only reply with `HEARTBEAT_OK` when there is truly nothing that should be surfaced to the user.
6. Do not explain internal heartbeat mechanics to the user.
