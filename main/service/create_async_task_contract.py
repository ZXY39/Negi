from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from main.models import build_execution_policy_schema, normalize_execution_policy_metadata


CREATE_ASYNC_TASK_DESCRIPTION = (
    "Dispatch the user request into a detached background task. "
    "The caller must provide a distilled `core_requirement` and an explicit "
    "`execution_policy.mode`. When the task depends on specific files or "
    "artifacts, `file_targets` must carry the exact absolute `path` or exact "
    "`ref` needed to reopen them."
)

CREATE_ASYNC_TASK_TASK_DESCRIPTION = (
    "Full task prompt for downstream execution. Describe the goal, scope, "
    "important clues, and expected output. If the task depends on files, also "
    "say in the prompt which files matter and how they should be used, but keep "
    "the exact reopen handles in `file_targets` instead of prose-only or bare "
    "filename references."
)

CREATE_ASYNC_TASK_CORE_REQUIREMENT_DESCRIPTION = (
    "One-sentence distilled core requirement for the entire task tree. This "
    "must not simply duplicate the full `task` text."
)

CREATE_ASYNC_TASK_EXECUTION_POLICY_DESCRIPTION = (
    "`focus` means highest-value direct work only; `coverage` still prioritizes "
    "highest-value work first, but allows broader follow-through when needed. "
    "Must be a JSON object like {\"mode\": \"focus\"} - never a JSON-encoded string."
)

CREATE_ASYNC_TASK_FILE_TARGETS_DESCRIPTION = (
    "Authoritative reopen targets for specific files or artifacts needed by the "
    "task. Use a list of objects with exact absolute `path` and/or exact `ref`; "
    "bare filenames like `resume.docx` are not valid reopen targets. When `path` "
    "is provided, runtime rejects relative paths and paths that do not point to "
    "an existing file. Use an empty list `[]` only when the task does not depend "
    "on specific files - never pass a JSON-encoded string here."
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


def validate_create_async_task_file_targets(value: Any) -> list[str]:
    normalized = normalize_create_async_task_file_targets(value)
    errors: list[str] = []
    for index, item in enumerate(normalized):
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            errors.append(f"file_targets[{index}].path must be an absolute path: {path}")
            continue
        if not candidate.exists():
            errors.append(f"file_targets[{index}].path does not exist: {path}")
            continue
        if not candidate.is_file():
            errors.append(f"file_targets[{index}].path must point to a file: {path}")
    return errors


def normalize_create_async_task_inbound_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Recover intended types for possibly string-serialized tool arguments.

    Some protocol bindings (e.g. qwen3.8-max via the DashScope Responses
    protocol) emit structured parameters as JSON strings - `execution_policy`
    as '{"mode":"focus"}' and `file_targets` as '[{"path": ...}]' or '[]'.
    Normalize them back to the object/array shapes the contract validates so
    the tool either executes correctly or fails with a precise error instead
    of a generic "should be array/object" rejection.
    """
    payload = dict(params or {})
    if "execution_policy" in payload:
        raw_policy = payload.get("execution_policy")
        parsed_policy: Any = raw_policy
        if isinstance(raw_policy, str):
            stripped_policy = str(raw_policy).strip()
            if stripped_policy.startswith("{") and stripped_policy.endswith("}"):
                try:
                    decoded = json.loads(stripped_policy)
                except Exception:
                    decoded = None
                if isinstance(decoded, dict):
                    parsed_policy = decoded
        payload["execution_policy"] = normalize_execution_policy_metadata(
            parsed_policy if isinstance(parsed_policy, dict) else raw_policy
        ).model_dump(mode="json")
    if "file_targets" in payload:
        raw_targets = payload.get("file_targets")
        if raw_targets is None:
            payload["file_targets"] = []
        elif isinstance(raw_targets, str):
            stripped_targets = str(raw_targets).strip()
            parsed_targets: Any = None
            if stripped_targets.startswith("[") and stripped_targets.endswith("]"):
                try:
                    parsed_targets = json.loads(stripped_targets)
                except Exception:
                    parsed_targets = None
            if isinstance(parsed_targets, list):
                payload["file_targets"] = parsed_targets
            else:
                # Not a JSON array: interpret the bare string semantically
                # (artifact: prefix → ref, otherwise → path) per the existing
                # file_targets normalization rules.
                payload["file_targets"] = normalize_create_async_task_file_targets(stripped_targets)
    return payload


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
                "type": "array",
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
