from __future__ import annotations

from typing import Any


TASK_APPEND_NOTICE_DESCRIPTION = (
    "Append new requirements, constraints, or acceptance details to an existing unfinished task "
    "in the current session. This updates existing work instead of creating a new task. "
    "Pass node_ids to target specific nodes: the notice is delivered into each target's subtree "
    "and only that subtree is frozen during distribution; task_ids (or omitting node_ids) targets "
    "the task root and freezes the whole tree."
)


def build_task_append_notice_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Target unfinished task ids in the current session; the notice targets each task's root node.",
            },
            "node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional targeted-notice node ids: each live execution node becomes a distribution "
                    "target and only its subtree is frozen while the notice is distributed downward. "
                    "Nested targets merge into the topmost ancestor. Terminal (success/failed) nodes and "
                    "acceptance nodes are rejected."
                ),
            },
            "message": {
                "type": "string",
                "minLength": 1,
                "description": "The appended notice, requirement, constraint, or acceptance detail.",
            },
        },
        "required": ["message"],
    }
