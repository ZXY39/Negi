"""silent 决策组的压缩退休规则：任务终态才退休，其余保守保留。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner


def _runner(status_by_task: dict[str, str], *, raises: bool = False):
    class _Service:
        def get_task(self, task_id: str):
            if raises:
                raise RuntimeError("store unavailable")
            status = status_by_task.get(str(task_id or "").strip())
            if status is None:
                return None
            return SimpleNamespace(status=status)

    return CeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=_Service()))


def _group(*subjects: str) -> list[dict]:
    tool_calls = []
    tool_results = []
    for index, subject in enumerate(subjects, start=1):
        call_id = f"call-silent-{index}"
        arguments = {"reason": "already covered", "subject": subject}
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "silent", "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        )
        tool_results.append(
            {"role": "tool", "tool_call_id": call_id, "name": "silent", "content": '{"silenced": true}'}
        )
    return [
        {"role": "assistant", "content": "", "tool_calls": tool_calls},
        *tool_results,
    ]


def test_success_and_failed_subjects_retire_the_whole_group() -> None:
    for status in ("success", "failed"):
        runner = _runner({"task:done": status})
        assert runner._retire_terminal_silent_messages(_group("task:done")) == []


def test_running_subject_is_kept() -> None:
    runner = _runner({"task:running": "in_progress"})
    group = _group("task:running")
    assert runner._retire_terminal_silent_messages(group) == group


def test_non_task_subject_is_kept() -> None:
    runner = _runner({})
    group = _group("event:heartbeat-1")
    assert runner._retire_terminal_silent_messages(group) == group


def test_unreadable_task_is_kept() -> None:
    runner = _runner({}, raises=True)
    group = _group("task:unknown")
    assert runner._retire_terminal_silent_messages(group) == group


def test_multi_subject_group_requires_every_task_terminal() -> None:
    runner = _runner({"task:a": "success", "task:b": "in_progress"})
    mixed = _group("task:a", "task:b")
    assert runner._retire_terminal_silent_messages(mixed) == mixed

    runner = _runner({"task:a": "success", "task:b": "failed"})
    assert runner._retire_terminal_silent_messages(_group("task:a", "task:b")) == []


def test_lift_then_retire_removes_the_whole_group_from_the_compressible_range() -> None:
    runner = _runner({"task:done": "success"})
    messages = [
        {"role": "user", "content": "bootstrap"},
        *_group("task:done"),
        {"role": "assistant", "content": "recent detail"},
    ]

    remaining, lifted = runner._lift_silent_trace_groups(messages)
    # lift 返回扁平列表：assistant 行 + 它声明的 tool 结果行。
    assert len(lifted) == 2
    assert len(remaining) == 2
    assert runner._retire_terminal_silent_messages(lifted) == []
    # 退休的组不回插：压缩后的正文里只剩 bootstrap 与 recent detail。
    assert [item["content"] for item in remaining] == ["bootstrap", "recent detail"]
