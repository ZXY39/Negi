"""Stop a runtime-owned background process.

`exec background=true` 起的进程在 exec 返回后仍然活着，而裸 shell 的终止形态被
宿主进程保护一律拒绝。这个工具是那条车道的出口：只认归属登记表里属于当前
session/task 的 pid，身份（创建时间）不符或不属于本 scope 一律拒绝。
"""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.runtime import owned_processes


class ProcessKillTool(Tool):
    # 终止是瞬时动作，不向模型暴露统一 timeout_seconds 参数。
    hide_universal_timeout_parameter = True

    @property
    def name(self) -> str:
        return "process_kill"

    @property
    def description(self) -> str:
        return (
            "Stop a pid started by exec background=true; only pids owned by this session/task are accepted."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pid": {
                    "type": "integer",
                    "description": "The pid returned by exec when it started the background process.",
                }
            },
            "required": ["pid"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        try:
            if int((params or {}).get("pid")) <= 0:
                errors.append("pid must be a positive integer")
        except (TypeError, ValueError):
            errors.append("pid must be a positive integer")
        return errors

    async def execute(self, pid: int, **kwargs: Any) -> str:
        runtime = kwargs.get("__g3ku_runtime")
        runtime_payload = runtime if isinstance(runtime, dict) else {}
        scope = owned_processes.scope_key(
            session_key=str(runtime_payload.get("session_key") or "web:shared"),
            task_id=str(runtime_payload.get("task_id") or ""),
        )
        try:
            normalized_pid = int(pid)
        except (TypeError, ValueError):
            return json.dumps(
                {"ok": False, "reason": "invalid_pid", "message": "pid must be a positive integer."},
                ensure_ascii=False,
            )

        result = owned_processes.kill_owned(scope, normalized_pid)
        payload: dict[str, Any] = {
            "ok": bool(result.get("ok")),
            "pid": normalized_pid,
            "status": str(result.get("reason") or ""),
        }
        if result.get("message"):
            payload["message"] = str(result["message"])
            # 名字对不上时给本 scope 的台账，模型不必猜 pid。
            payload["owned_processes"] = [
                {
                    "pid": int(entry.get("pid") or 0),
                    "cmd": str(entry.get("cmd") or "")[:200],
                    "log_path": str(entry.get("log_path") or ""),
                }
                for entry in owned_processes.list_owned(scope)[:10]
            ]
        entry = result.get("entry")
        if isinstance(entry, dict):
            payload["cmd"] = str(entry.get("cmd") or "")[:200]
            payload["log_path"] = str(entry.get("log_path") or "")
        if result.get("log_tail"):
            payload["log_tail"] = str(result["log_tail"])
        return json.dumps(payload, ensure_ascii=False)
