"""Stop runtime-owned background processes.

`exec background=true` 起的进程在 exec 返回后仍然活着，而裸 shell 的终止形态被
宿主进程保护一律拒绝。这个工具是那条车道的出口：只认归属登记表里属于当前
session/task 的 pid，身份（创建时间）不符或不属于本 scope 一律拒绝。单 pid 与
`pids` 批量走同一条归属校验；批量逐项裁决，一个失败不会短路其余项。
"""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.runtime import owned_processes

_DESCRIPTION = (
    "Stop one or more pids started by exec background=true; only pids owned by this "
    "session/task are accepted. Pass pid for a single process or pids for a batch; a "
    "batch judges every pid independently and reports one result per pid."
)


def _kill_result_payload(pid: int, result: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pid": pid,
        "ok": bool(result.get("ok")),
        "status": str(result.get("reason") or ""),
    }
    if result.get("message"):
        payload["message"] = str(result["message"])
    entry = result.get("entry")
    if isinstance(entry, dict):
        payload["cmd"] = str(entry.get("cmd") or "")[:200]
        payload["log_path"] = str(entry.get("log_path") or "")
        if entry.get("label"):
            payload["purpose"] = str(entry.get("label") or "")
    if result.get("log_tail"):
        payload["log_tail"] = str(result["log_tail"])
    if result.get("leftover_descendants"):
        payload["leftover_descendants"] = list(result["leftover_descendants"])
    return payload


def _batch_status(results: list[dict[str, Any]]) -> str:
    """全成 = terminated，部分成 = partial；全败时同一原因原样透出，否则 failed。"""
    if all(bool(item.get("ok")) for item in results):
        return "terminated"
    if any(bool(item.get("ok")) for item in results):
        return "partial"
    statuses = {str(item.get("status") or "") for item in results}
    return statuses.pop() if len(statuses) == 1 else "failed"


def _normalize_requested_pids(pid: Any, pids: Any) -> tuple[list[int], str | None]:
    requested: list[int] = []

    def add(value: Any, label: str) -> str | None:
        if isinstance(value, bool):
            return f"{label} must be a positive integer."
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return f"{label} must be a positive integer."
        if normalized <= 0:
            return f"{label} must be a positive integer."
        if normalized not in requested:
            requested.append(normalized)
        return None

    if pid is not None:
        error = add(pid, "pid")
        if error is not None:
            return [], error
    if pids is not None:
        if not isinstance(pids, (list, tuple)):
            return [], "pids must be an array of positive integers."
        if not pids:
            return [], "pids must contain at least one pid."
        for index, item in enumerate(pids):
            error = add(item, f"pids[{index}]")
            if error is not None:
                return [], error
    if not requested:
        return [], "process_kill requires pid or pids."
    return requested, None


class ProcessKillTool(Tool):
    # 终止是瞬时动作，不向模型暴露统一 timeout_seconds 参数。
    hide_universal_timeout_parameter = True

    @property
    def name(self) -> str:
        return "process_kill"

    @property
    def description(self) -> str:
        return _DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pid": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "The pid returned by exec when it started the background process.",
                },
                "pids": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "integer", "minimum": 1},
                    "description": (
                        "Several pids returned by exec background=true calls. Every pid is "
                        "judged independently; one failure does not stop the rest."
                    ),
                },
            },
            "required": [],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        payload = params if isinstance(params, dict) else {}
        if payload.get("pid") is None and payload.get("pids") is None:
            return ["pid or pids is required"]
        return super().validate_params(payload)

    async def execute(
        self,
        pid: int | None = None,
        pids: list[Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = kwargs.get("__g3ku_runtime")
        runtime_payload = runtime if isinstance(runtime, dict) else {}
        scope = owned_processes.scope_key(
            session_key=str(runtime_payload.get("session_key") or "web:shared"),
            task_id=str(runtime_payload.get("task_id") or ""),
        )
        node_id = str(runtime_payload.get("node_id") or "").strip()
        requested, error = _normalize_requested_pids(pid, pids)
        if error is not None:
            return json.dumps(
                {
                    "ok": False,
                    "status": "invalid_pid",
                    "reason": "invalid_pid",
                    "message": error,
                },
                ensure_ascii=False,
            )

        results = [
            _kill_result_payload(
                requested_pid,
                owned_processes.kill_owned(scope, requested_pid, node_id=node_id or None),
            )
            for requested_pid in requested
        ]
        notify = runtime_payload.get("_notify_background_items_changed")
        if callable(notify):
            try:
                notify()
            except Exception:
                pass

        if pids is None:
            payload = results[0]
        else:
            payload = {
                "ok": all(bool(item.get("ok")) for item in results),
                "pids": requested,
                "status": _batch_status(results),
                "results": results,
            }
        if any(item.get("message") for item in results):
            # 名字对不上时给本节点的台账，模型不必猜 pid；legacy 空归属条目一并带出。
            payload["owned_processes"] = [
                {
                    "pid": int(entry.get("pid") or 0),
                    "purpose": str(entry.get("label") or ""),
                    "cmd": str(entry.get("cmd") or "")[:200],
                    "log_path": str(entry.get("log_path") or ""),
                }
                for entry in owned_processes.list_owned(
                    scope,
                    node_id=node_id or None,
                    include_unattributed=True,
                )[:10]
            ]
        return json.dumps(payload, ensure_ascii=False)
