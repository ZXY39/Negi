from __future__ import annotations

import json

from g3ku.runtime.frontdoor.turns import build_frontdoor_turn_tools
from g3ku.runtime.tool_result_status import is_error_like_tool_result

# exec 的失败是**结构化载荷自己声明**的（`g3ku/agent/tools/shell.py` 按退出码写 status），
# 判据只认 "Error" 前缀和 `ok:false` 时，这条载荷读成成功：回合账本落 `success`，
# 在飞行的感叹号被账本里的 √ 覆盖。


def _exec_payload(*, status: str, exit_code: int | None, error: str = "") -> str:
    payload = {
        "status": status,
        "exit_code": exit_code,
        "head_preview": "STDERR: command not found",
        "tail_preview": "",
    }
    if error:
        payload["error"] = error
    return json.dumps(payload, ensure_ascii=False)


def test_nonzero_exit_payload_is_an_error_result() -> None:
    assert is_error_like_tool_result(
        _exec_payload(status="error", exit_code=1, error="Exit code: 1")
    ) is True


def test_zero_exit_payload_is_not_an_error_result() -> None:
    assert is_error_like_tool_result(_exec_payload(status="success", exit_code=0)) is False


def test_plain_error_text_still_counts() -> None:
    assert is_error_like_tool_result("Error: Command blocked by safety guard") is True


def test_action_state_payload_is_not_read_as_a_failed_call() -> None:
    """`status` 在别的工具里写的是**动作落点**，不是本次调用的成败。

    `stop_tool_execution` 成功取消任务时回 `{"status": "stopped", ...}`，`process_control`
    成败写在 `ok` 上、`status` 放原因。把它们判成失败会把一次成功的停止画成感叹号。
    """
    assert is_error_like_tool_result(json.dumps({"status": "stopped", "task_id": "task:a"})) is False
    assert is_error_like_tool_result(json.dumps({"status": "not_found", "message": "no such task"})) is False
    assert is_error_like_tool_result(json.dumps({"ok": True, "status": "killed", "pid": 4321})) is False


def test_explicit_failure_marker_still_counts() -> None:
    assert is_error_like_tool_result(json.dumps({"ok": False, "error": "disk full"})) is True


def test_ledger_records_the_exec_failure() -> None:
    """账本这一行是两条显示车道的共同来源：它落 success，轨道上就是 √。

    状态由执行侧那颗判据算出再交给账本构造器（`_run_tool_and_render` → `_tool_status`），
    所以这里逐字重放同一条道，而不是把一个现成的 status 塞给构造器。
    """
    from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport

    support = object.__new__(CeoFrontDoorSupport)
    result_text = _exec_payload(status="error", exit_code=1, error="Exit code: 1")

    assert support._tool_status(result_text) == "error"

    tools = build_frontdoor_turn_tools(
        [{"id": "call-1", "name": "exec", "arguments": {"command": "Get-ChildItem"}}],
        [{"tool_name": "exec", "status": support._tool_status(result_text), "result_text": result_text}],
    )

    assert tools[0]["status"] == "error"
