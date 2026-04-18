from __future__ import annotations

from typing import Any


TASK_APPEND_NOTICE_DESCRIPTION = (
    "Append new requirements, constraints, or acceptance details to an existing unfinished task "
    "in the current session. This updates existing work instead of creating a new task."
)


def build_task_append_notice_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Target unfinished task ids in the current session.",
            },
            "node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional live-tree node ids that resolve back to unfinished tasks in the current session.",
            },
            "message": {
                "type": "string",
                "minLength": 1,
                "description": "The appended notice, requirement, constraint, or acceptance detail.",
            },
        },
        "required": ["message"],
    }
