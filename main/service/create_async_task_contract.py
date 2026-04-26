from __future__ import annotations

from typing import Any

from main.models import build_execution_policy_schema


CREATE_ASYNC_TASK_DESCRIPTION = (
    "Dispatch the user request into a detached background task. "
    "The caller must provide a distilled `core_requirement` and an explicit "
    "`execution_policy.mode`. When the task depends on specific files or "
    "artifacts, put the exact reopen targets into `file_targets`."
)

CREATE_ASYNC_TASK_TASK_DESCRIPTION = (
    "Full task prompt for downstream execution. Describe the goal, scope, "
    "important clues, and expected output. If the task depends on files, also "
    "say in the prompt which files matter and how they should be used."
)

CREATE_ASYNC_TASK_CORE_REQUIREMENT_DESCRIPTION = (
    "One-sentence distilled core requirement for the entire task tree. This "
    "must not simply duplicate the full `task` text."
)

CREATE_ASYNC_TASK_EXECUTION_POLICY_DESCRIPTION = (
    "`focus` means highest-value direct work only; `coverage` still prioritizes "
    "highest-value work first, but allows broader follow-through when needed."
)

CREATE_ASYNC_TASK_FILE_TARGETS_DESCRIPTION = (
    "Optional reopen targets for specific files or artifacts needed by the task. "
    "Use a list of objects with exact `path` and/or exact `ref`. Use `null` or "
    "an empty list when the task does not depend on specific files."
)

CREATE_ASYNC_TASK_REQUIRES_FINAL_ACCEPTANCE_DESCRIPTION = (
    "Whether the root execution should be followed by final acceptance."
)

CREATE_ASYNC_TASK_FINAL_ACCEPTANCE_PROMPT_DESCRIPTION = (
    "Final acceptance instructions. Required only when "
    "`requires_final_acceptance=true`."
)


def normalize_create_async_task_file_targets(value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []

    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = list(value)
    elif isinstance(value, dict):
        raw_items = [dict(value)]
    elif isinstance(value, str):
        raw_items = [value]
    else:
        return []

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_items:
        path = ""
        ref = ""
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            ref = str(item.get("ref") or "").strip()
        elif isinstance(item, str):
            text = str(item or "").strip()
            if not text:
                continue
            if text.startswith("artifact:"):
                ref = text
            else:
                path = text
        if not path and not ref:
            continue
        key = (path, ref)
        if key in seen:
            continue
        seen.add(key)
        payload: dict[str, str] = {}
        if path:
            payload["path"] = path
        if ref:
            payload["ref"] = ref
        normalized.append(payload)
    return normalized


def build_create_async_task_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": CREATE_ASYNC_TASK_TASK_DESCRIPTION,
            },
            "core_requirement": {
                "type": "string",
                "description": CREATE_ASYNC_TASK_CORE_REQUIREMENT_DESCRIPTION,
            },
            "execution_policy": build_execution_policy_schema(
                description=CREATE_ASYNC_TASK_EXECUTION_POLICY_DESCRIPTION,
            ),
            "file_targets": {
                "type": ["array", "null"],
                "description": CREATE_ASYNC_TASK_FILE_TARGETS_DESCRIPTION,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": ["string", "null"],
                            "description": "Exact absolute file path when a local file should be reopened.",
                        },
                        "ref": {
                            "type": ["string", "null"],
                            "description": "Exact artifact/content reference when the file should be reopened by ref.",
                        },
                    },
                },
            },
            "requires_final_acceptance": {
                "type": "boolean",
                "description": CREATE_ASYNC_TASK_REQUIRES_FINAL_ACCEPTANCE_DESCRIPTION,
            },
            "final_acceptance_prompt": {
                "type": "string",
                "description": CREATE_ASYNC_TASK_FINAL_ACCEPTANCE_PROMPT_DESCRIPTION,
            },
        },
        "required": ["task", "core_requirement", "execution_policy"],
    }
