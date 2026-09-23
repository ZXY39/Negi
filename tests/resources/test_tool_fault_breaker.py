from __future__ import annotations

from pathlib import Path

import main.errors as errors_module
from main.errors import is_runtime_self_fault
from main.runtime.react_loop import ReActToolLoop, _TOOL_FAULT_LIMIT


def _raise_with_frame(*, exc_name: str, filename: str) -> BaseException:
    """抛一个"最后一帧落在 filename"的异常，用来伪造异常归属地。"""
    code = compile(f"raise {exc_name}('boom from {exc_name}')", filename, "exec")
    try:
        exec(code, {})  # noqa: S102 - 测试内受控编译
    except BaseException as exc:  # noqa: BLE001 - 正是要拿到带 traceback 的异常
        return exc
    raise AssertionError('unreachable')


def _owned(subpath: str) -> str:
    root = Path(errors_module.__file__).resolve().parent.parent
    return str(root / subpath)


def test_runtime_self_fault_accepts_engine_defect_inside_runtime_packages() -> None:
    exc = _raise_with_frame(exc_name='NameError', filename=_owned('main/monitoring/log_service.py'))
    assert is_runtime_self_fault(exc) is True


def test_runtime_self_fault_rejects_tool_own_validation_error() -> None:
    # 炸在 tools/ 里是工具实现的输入校验，模型自己消化即可，不该熔断。
    exc = _raise_with_frame(exc_name='TypeError', filename=_owned('tools/agent_browser/main/tool.py'))
    assert is_runtime_self_fault(exc) is False


def test_runtime_self_fault_rejects_types_outside_the_set() -> None:
    exc = _raise_with_frame(exc_name='ValueError', filename=_owned('main/runtime/react_loop.py'))
    assert is_runtime_self_fault(exc) is False


def test_runtime_self_fault_rejects_exception_without_traceback() -> None:
    assert is_runtime_self_fault(NameError('no traceback attached')) is False
    assert is_runtime_self_fault(None) is False


def _fault_result(*, tool_name: str, fault: str) -> dict[str, object]:
    return {
        'live_state': {'tool_name': tool_name},
        'runtime_fault': fault,
    }


def _signature(tool_name: str) -> str:
    return f'runtime_fault:{tool_name}:NameError: boom from NameError'


def test_tool_fault_signature_only_for_runtime_self_faults() -> None:
    exc = _raise_with_frame(exc_name='NameError', filename=_owned('main/runtime/node_runner.py'))
    assert ReActToolLoop._runtime_fault_signature(tool_name='submit_next_stage', exc=exc) == _signature('submit_next_stage')
    ordinary = _raise_with_frame(exc_name='ValueError', filename=_owned('tools/x/main/tool.py'))
    assert ReActToolLoop._runtime_fault_signature(tool_name='exec', exc=ordinary) == ''


def test_tool_fault_breaker_trips_on_repeated_identical_fault() -> None:
    """实盘形态：18 次同文本 NameError 中间夹着别的工具成功，仍必须熔断。"""
    counts: dict[str, int] = {}
    hit = None
    for _ in range(_TOOL_FAULT_LIMIT - 1):
        hit = ReActToolLoop._register_tool_fault_results(
            results=[_fault_result(tool_name='submit_next_stage', fault=_signature('submit_next_stage'))],
            fault_counts=counts,
        )
        assert hit is None
        # 同一轮里 exec 拿到宽限成功——不得打断计数。
        ReActToolLoop._register_tool_fault_results(
            results=[_fault_result(tool_name='exec', fault='')],
            fault_counts=counts,
        )
    hit = ReActToolLoop._register_tool_fault_results(
        results=[_fault_result(tool_name='submit_next_stage', fault=_signature('submit_next_stage'))],
        fault_counts=counts,
    )
    assert hit is not None
    assert hit['count'] == _TOOL_FAULT_LIMIT
    assert hit['signature'] == _signature('submit_next_stage')


def test_tool_fault_counter_drops_signature_when_that_tool_recovers() -> None:
    counts: dict[str, int] = {}
    for _ in range(2):
        ReActToolLoop._register_tool_fault_results(
            results=[_fault_result(tool_name='submit_next_stage', fault=_signature('submit_next_stage'))],
            fault_counts=counts,
        )
    assert counts
    ReActToolLoop._register_tool_fault_results(
        results=[_fault_result(tool_name='submit_next_stage', fault='')],
        fault_counts=counts,
    )
    assert counts == {}


def test_tool_fault_failure_routes_into_error_pause_lane() -> None:
    """撞上限必须产出 failure_disposition='pause' 且正文带 runtime_fault: 标记。

    `node_runner.run_node` 只认 disposition，心跳分型只认错误文本里的标记，
    两者缺一都会让这条道退回成"静默空转"。
    """
    result = ReActToolLoop._tool_fault_failure(signature=_signature('submit_next_stage'), count=_TOOL_FAULT_LIMIT)
    assert result.status == 'failed'
    assert result.failure_disposition == 'pause'
    assert 'runtime_fault:' in result.failure_text
    assert '重启 worker' in result.failure_text
