from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.agent.tools.base import Tool
from g3ku.providers.base import LLMResponse, ToolCallRequest
from main.protocol import now_iso
from main.prompts import load_prompt
from main.runtime.internal_tools import SubmitFinalResultTool
from main.runtime.react_loop import ReActToolLoop

_CONTRACT_MARKER = '"message_type": "node_runtime_tool_contract"'
_OVERLAY_MARKER = "\n\nSystem note for this turn only:\n"


def _normalized_names(items: list[Any] | None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(items or []):
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


class _FakeTaskStore:
    def __init__(self, *, task: Any, node: Any) -> None:
        self._task = task
        self._node = node

    def get_task(self, task_id: str) -> Any:
        _ = task_id
        return self._task

    def get_node(self, node_id: str) -> Any:
        _ = node_id
        return self._node


class _RecordingLogService:
    def __init__(self, *, task: Any, node: Any, stage_gate: dict[str, Any]) -> None:
        self._store = _FakeTaskStore(task=task, node=node)
        self._frames: dict[tuple[str, str], dict[str, Any]] = {}
        self._stage_gate = dict(stage_gate or {})
        self.node_inputs: list[dict[str, Any]] = []
        self._round_counter = 0
        self._latest_node_input_messages: list[dict[str, Any]] = []

    def set_pause_state(self, task_id: str, pause_requested: bool, is_paused: bool) -> None:
        _ = task_id, pause_requested, is_paused

    def update_node_input(self, task_id: str, node_id: str, payload: str) -> None:
        parsed_messages: list[dict[str, Any]] = []
        try:
            parsed = json.loads(str(payload or "[]"))
            if isinstance(parsed, list):
                parsed_messages = [dict(item) for item in parsed if isinstance(item, dict)]
        except Exception:
            parsed_messages = []
        self.node_inputs.append(
            {
                "task_id": str(task_id or "").strip(),
                "node_id": str(node_id or "").strip(),
                "messages": parsed_messages,
            }
        )
        self._latest_node_input_messages = [dict(item) for item in parsed_messages]

    def upsert_frame(self, task_id: str, payload: dict[str, Any], publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        node_id = str((payload or {}).get("node_id") or "").strip()
        key = (str(task_id or "").strip(), node_id)
        current = dict(self._frames.get(key) or {})
        current.update(dict(payload or {}))
        self._frames[key] = current

    def append_node_output(self, *args: Any, **kwargs: Any) -> None:
        task_id, node_id = args[:2]
        node = self._store.get_node(str(node_id or "").strip())
        if node is None:
            return None
        outputs = list(getattr(node, "output", []) or [])
        outputs.append(
            SimpleNamespace(
                created_at=now_iso(),
                content=str(kwargs.get("content") or ""),
                content_ref="",
                tool_calls=[dict(item) for item in list(kwargs.get("tool_calls") or []) if isinstance(item, dict)],
            )
        )
        node.output = outputs
        _ = task_id
        return node

    def update_frame(self, task_id: str, node_id: str, mutate, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        key = (str(task_id or "").strip(), str(node_id or "").strip())
        current = dict(self._frames.get(key) or {})
        updated = mutate(current)
        self._frames[key] = dict(updated or {})

    def remove_frame(self, task_id: str, node_id: str, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        self._frames.pop((str(task_id or "").strip(), str(node_id or "").strip()), None)

    def read_runtime_frame(self, task_id: str, node_id: str) -> dict[str, Any]:
        return dict(self._frames.get((str(task_id or "").strip(), str(node_id or "").strip())) or {})

    def execution_stage_gate_snapshot(self, task_id: str, node_id: str) -> dict[str, Any]:
        _ = task_id, node_id
        return copy.deepcopy(self._stage_gate)

    def latest_node_input_messages(self) -> list[dict[str, Any]]:
        return [dict(item) for item in list(self._latest_node_input_messages or []) if isinstance(item, dict)]

    def record_execution_stage_round(
        self,
        task_id: str,
        node_id: str,
        *,
        tool_calls: list[dict[str, Any]],
        created_at: str,
    ) -> dict[str, Any]:
        _ = task_id, node_id
        self._round_counter += 1
        return {
            "round_id": f"round-{self._round_counter}",
            "created_at": str(created_at or now_iso()),
            "tool_calls": [dict(item) for item in list(tool_calls or []) if isinstance(item, dict)],
        }


class _RecordingChatBackend:
    def __init__(self, *, log_service: _RecordingLogService) -> None:
        self.calls: list[dict[str, Any]] = []
        self._log_service = log_service

    async def chat(self, **kwargs: Any) -> LLMResponse:
        request = _serializable_chat_request(kwargs)
        request["model_messages"] = self._log_service.latest_node_input_messages()
        self.calls.append(request)
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call:load-tool-context",
                        name="load_tool_context",
                        arguments={"tool_id": "filesystem_write"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"input_tokens": 64, "output_tokens": 16},
            )
        if len(self.calls) == 2:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call:submit-final-result",
                        name="submit_final_result",
                        arguments={
                            "status": "success",
                            "delivery_status": "final",
                            "summary": "Prompt export completed.",
                            "answer": "Prompt export completed.",
                            "evidence": [{"kind": "artifact", "note": "captured request payloads"}],
                            "remaining_work": [],
                            "blocking_reason": "",
                        },
                    )
                ],
                finish_reason="tool_calls",
                usage={"input_tokens": 64, "output_tokens": 16},
            )
        raise AssertionError("recording backend only supports two model turns")

    def recommended_model_response_timeout_seconds(self, *, model_refs: list[str]) -> None:
        _ = model_refs
        return None


class _LoadToolContextTool(Tool):
    @property
    def name(self) -> str:
        return "load_tool_context"

    @property
    def description(self) -> str:
        return "Load tool contract details for a candidate tool."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tool_id": {
                    "type": "string",
                    "description": "Candidate tool id to load.",
                }
            },
            "required": ["tool_id"],
        }

    async def execute(self, *, tool_id: str, **kwargs: Any) -> str:
        _ = kwargs
        return json.dumps(
            {
                "ok": True,
                "tool_id": str(tool_id or "").strip(),
                "description": "Write a file to the workspace.",
            },
            ensure_ascii=False,
        )


class _FilesystemWriteTool(Tool):
    @property
    def name(self) -> str:
        return "filesystem_write"

    @property
    def description(self) -> str:
        return "Write a file to the workspace."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute target path.",
                },
                "content": {
                    "type": "string",
                    "description": "File content to write.",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, *, path: str, content: str, **kwargs: Any) -> str:
        _ = path, content, kwargs
        return json.dumps({"ok": True}, ensure_ascii=False)


def _stage_gate_payload() -> dict[str, Any]:
    return {
        "has_active_stage": True,
        "transition_required": False,
        "active_stage": {
            "stage_id": "stage-1",
            "stage_goal": "Inspect the repo and prepare a small file write.",
            "tool_round_budget": 6,
            "tool_rounds_used": 0,
            "stage_kind": "normal",
            "final_stage": False,
            "mode": "self",
            "status": "active",
            "rounds": [],
        },
        "completed_stages": [],
    }


def _initial_messages(*, task_id: str, node_id: str, output_dir: Path, stage_gate: dict[str, Any]) -> list[dict[str, str]]:
    user_payload = {
        "task_id": task_id,
        "node_id": node_id,
        "node_kind": "execution",
        "depth": 0,
        "can_spawn_children": False,
        "goal": "Inspect the repo and prepare to write a file.",
        "prompt": "Inspect the repo and prepare to write a small note file.",
        "core_requirement": "Inspect the repo and prepare to write a small note file.",
        "execution_policy": {"mode": "focus"},
        "runtime_environment": {
            "workspace_root": str(Path.cwd().resolve()),
            "task_temp_dir": str(output_dir.resolve()),
            "project_python_hint": "python",
            "path_policy": {
                "workspace_root": str(Path.cwd().resolve()),
                "working_directory": str(Path.cwd().resolve()),
                "require_absolute_paths": True,
            },
            "tool_guidance": {
                "preferred_read_tool": "content_open",
                "preferred_write_tool": "filesystem_write",
            },
        },
    }
    return [
        {"role": "system", "content": load_prompt("node_execution.md").strip()},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, indent=2),
        },
    ]


def _seed_runtime_frame(log_service: _RecordingLogService, *, task_id: str, node_id: str) -> None:
    log_service.upsert_frame(
        task_id,
        {
            "node_id": node_id,
            "depth": 0,
            "node_kind": "execution",
            "phase": "before_model",
            "messages": [],
            "visible_skills": [
                {
                    "skill_id": "tmux",
                    "display_name": "tmux",
                    "description": "Terminal workflow helper.",
                }
            ],
            "selected_skill_ids": ["tmux"],
            "candidate_skill_ids": ["tmux"],
            "candidate_skill_items": [
                {
                    "skill_id": "tmux",
                    "description": "Terminal workflow helper.",
                }
            ],
            "candidate_tool_names": ["filesystem_write"],
            "rbac_visible_tool_names": ["load_tool_context", "filesystem_write", "submit_final_result"],
            "rbac_visible_skill_ids": ["tmux"],
            "hydrated_executor_state": [],
            "hydrated_executor_names": [],
        },
        publish_snapshot=False,
    )


def _serializable_chat_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": [dict(item) for item in list(kwargs.get("messages") or []) if isinstance(item, dict)],
        "tools": copy.deepcopy(list(kwargs.get("tools") or [])),
        "model_refs": list(kwargs.get("model_refs") or []),
        "tool_choice": copy.deepcopy(kwargs.get("tool_choice")),
        "parallel_tool_calls": kwargs.get("parallel_tool_calls"),
        "prompt_cache_key": kwargs.get("prompt_cache_key"),
    }


def _extract_dynamic_contract_payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    for message in list(messages or []):
        if str((message or {}).get("role") or "").strip().lower() != "user":
            continue
        raw_content = str((message or {}).get("content") or "")
        if _CONTRACT_MARKER not in raw_content:
            continue
        contract_content = raw_content.split(_OVERLAY_MARKER, 1)[0]
        payload = json.loads(contract_content)
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("node runtime tool contract not found in request messages")


def _render_messages_markdown(messages: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for index, message in enumerate(list(messages or []), start=1):
        role = str((message or {}).get("role") or "").strip() or "unknown"
        content = str((message or {}).get("content") or "")
        sections.append(f"## Message {index} ({role})\n\n```text\n{content}\n```")
    return "# Request Messages\n\n" + "\n\n".join(sections) + "\n"


def _dynamic_contract_message_indexes(messages: list[dict[str, Any]]) -> list[int]:
    indexes: list[int] = []
    for index, message in enumerate(list(messages or [])):
        if str((message or {}).get("role") or "").strip().lower() != "user":
            continue
        if _CONTRACT_MARKER in str((message or {}).get("content") or ""):
            indexes.append(index)
    return indexes


def _build_selector(log_service: _RecordingLogService):
    def _selector(
        *,
        task_id: str,
        node_id: str,
        node_kind: str,
        visible_tools: dict[str, Tool],
        runtime_context: dict[str, Any],
    ) -> dict[str, Any]:
        _ = node_kind, runtime_context
        frame = log_service.read_runtime_frame(task_id, node_id)
        hydrated = _normalized_names(
            list(frame.get("hydrated_executor_state") or []) or list(frame.get("hydrated_executor_names") or [])
        )
        callable_tool_names = ["load_tool_context", "submit_final_result"]
        candidate_tool_names = ["filesystem_write"]
        if "filesystem_write" in hydrated:
            callable_tool_names = ["load_tool_context", "filesystem_write", "submit_final_result"]
            candidate_tool_names = []
        callable_tool_names = [name for name in callable_tool_names if name in visible_tools]
        candidate_tool_names = [name for name in candidate_tool_names if name in visible_tools]
        return {
            "tool_names": list(callable_tool_names),
            "candidate_tool_names": list(candidate_tool_names),
            "hydrated_executor_names": list(hydrated),
            "lightweight_tool_ids": ["filesystem"],
            "trace": {
                "mode": "export_node_prompt_rounds",
                "full_callable_tool_names": list(callable_tool_names),
                "stage_locked_to_submit_next_stage": False,
            },
        }

    return _selector


def _build_hydration_promoter(log_service: _RecordingLogService):
    def _promoter(*, task_id: str, node_id: str, raw_result: dict[str, Any], **kwargs: Any) -> None:
        _ = kwargs
        tool_id = str((raw_result or {}).get("tool_id") or "").strip()
        if not tool_id:
            return

        def _mutate(frame: dict[str, Any]) -> dict[str, Any]:
            hydrated = _normalized_names(
                list(frame.get("hydrated_executor_state") or []) or list(frame.get("hydrated_executor_names") or [])
            )
            if tool_id not in hydrated:
                hydrated.append(tool_id)
            return {
                **dict(frame or {}),
                "hydrated_executor_state": list(hydrated),
                "hydrated_executor_names": list(hydrated),
            }

        log_service.update_frame(task_id, node_id, _mutate, publish_snapshot=False)

    return _promoter


async def _export_rounds(*, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    task = SimpleNamespace(
        task_id="task-node-prompt-export",
        status="in_progress",
        cancel_requested=False,
        pause_requested=False,
        failure_reason="",
    )
    node = SimpleNamespace(
        task_id=task.task_id,
        node_id="node-node-prompt-export",
        depth=0,
        node_kind="execution",
        can_spawn_children=False,
        metadata={},
        output=[],
    )
    stage_gate = _stage_gate_payload()
    log_service = _RecordingLogService(task=task, node=node, stage_gate=stage_gate)
    _seed_runtime_frame(log_service, task_id=task.task_id, node_id=node.node_id)

    backend = _RecordingChatBackend(log_service=log_service)
    loop = ReActToolLoop(chat_backend=backend, log_service=log_service, max_iterations=4)
    loop._model_visible_tool_schema_selector = _build_selector(log_service)
    loop._tool_context_hydration_promoter = _build_hydration_promoter(log_service)

    async def _submit(payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload or {})

    tools: dict[str, Tool] = {
        "load_tool_context": _LoadToolContextTool(),
        "filesystem_write": _FilesystemWriteTool(),
        "submit_final_result": SubmitFinalResultTool(_submit, node_kind="execution"),
    }

    await loop.run(
        task=task,
        node=node,
        messages=_initial_messages(
            task_id=task.task_id,
            node_id=node.node_id,
            output_dir=output_dir,
            stage_gate=stage_gate,
        ),
        tools=tools,
        tools_supplier=lambda: dict(tools),
        model_refs=["execution"],
        model_refs_supplier=lambda: ["execution"],
        runtime_context={
            "task_id": task.task_id,
            "node_id": node.node_id,
            "node_kind": node.node_kind,
            "session_key": "web:shared",
            "actor_role": "execution",
        },
        max_iterations=4,
    )

    if len(backend.calls) != 2:
        raise RuntimeError(f"expected exactly 2 captured model requests, got {len(backend.calls)}")

    summary = {
        "task_id": task.task_id,
        "node_id": node.node_id,
        "node_kind": node.node_kind,
        "round_count": len(backend.calls),
        "rounds": [],
    }

    for index, request in enumerate(backend.calls, start=1):
        round_prefix = f"round{index}"
        model_messages = [dict(item) for item in list(request.get("model_messages") or []) if isinstance(item, dict)]
        request_messages = list(request.get("messages") or [])
        dynamic_contract = _extract_dynamic_contract_payload(request_messages)

        (output_dir / f"{round_prefix}.request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / f"{round_prefix}.model_messages.json").write_text(
            json.dumps(model_messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / f"{round_prefix}.dynamic_contract.json").write_text(
            json.dumps(dynamic_contract, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / f"{round_prefix}.messages.md").write_text(
            _render_messages_markdown(request_messages),
            encoding="utf-8",
        )

        summary["rounds"].append(
            {
                "round": index,
                "request_file": f"{round_prefix}.request.json",
                "model_messages_file": f"{round_prefix}.model_messages.json",
                "dynamic_contract_file": f"{round_prefix}.dynamic_contract.json",
                "message_roles": [
                    str((message or {}).get("role") or "")
                    for message in request_messages
                ],
                "dynamic_contract_message_indexes": _dynamic_contract_message_indexes(request_messages),
                "dynamic_contract_message_count": sum(
                    1
                    for message in request_messages
                    if str((message or {}).get("role") or "").strip().lower() == "user"
                    and _CONTRACT_MARKER in str((message or {}).get("content") or "")
                ),
                "last_message_role": (
                    str((request_messages[-1] or {}).get("role") or "")
                    if request_messages
                    else ""
                ),
                "callable_tool_names": list(dynamic_contract.get("callable_tool_names") or []),
                "candidate_tools": list(dynamic_contract.get("candidate_tools") or []),
                "candidate_skills": list(dynamic_contract.get("candidate_skills") or []),
            }
        )

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate two consecutive node model requests and export the complete prompt payloads.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("temp") / "node-prompt-rounds",
        help="Directory that will receive the exported prompt files.",
    )
    args = parser.parse_args()

    summary = asyncio.run(_export_rounds(output_dir=args.output_dir))
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir.resolve()),
                "round_count": summary["round_count"],
                "files": [
                    item["request_file"]
                    for item in list(summary.get("rounds") or [])
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
