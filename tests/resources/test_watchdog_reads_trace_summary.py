"""agent 端"最近工具调用"在快照只带摘要时仍要有读者，且必须按轮次 seq 排。

任务详情快照不再内联轨迹正文（1.82 MB 里 1.42 MB 那一块）之后，
`summarize_runtime_snapshot` 原来的两级读者是 `execution_trace.tool_steps` → 运行帧；
中间那级没了来源，agent 看到的最近工具会直接掉到帧上。这里补摘要读者作为第二级。

夹具走**生产函数** `build_execution_trace_summary`：摘要的形状由它单源决定，
手写一份"看起来像"的字典会让这条用例在形状漂移时照样绿（前一版就是这么写歪的）。
"""

from g3ku.runtime.tool_watchdog import summarize_runtime_snapshot
from main.runtime.execution_trace_compaction import build_execution_trace_summary


def _payload(root_node: dict) -> dict:
    return {
        'task': {'task_id': 'task:watchdog', 'status': 'in_progress'},
        'root_node': {'node_id': 'node:root', 'goal': 'do the thing', 'status': 'in_progress', **root_node},
        'frontier': [],
    }


def _summary_from_trace(turns: list[dict]) -> dict:
    """把轨迹交给真正的写侧构造器，摘要形状与生产一致。"""
    return build_execution_trace_summary({'turns': turns, 'tool_steps': []})


def _tool(name: str, status: str, output_text: str) -> dict:
    return {'tool_name': name, 'status': status, 'arguments': {}, 'output_text': output_text}


# 落盘顺序故意打乱：最新的一轮排在第一位，最旧的排在中间。名字按"真实先后"取，
# 所以断言读到的顺序应当是 oldest → middle → newest，而不是它们被存下来的样子。
_SUMMARY_TURNS_SHUFLED = _summary_from_trace([
    {'seq': 5, 'tools': [_tool('newest', 'running', 'c')]},
    {'seq': 1, 'tools': [_tool('oldest', 'success', 'a')]},
    {'seq': 3, 'tools': [_tool('middle', 'success', 'b')]},
])


def test_summary_only_root_node_still_yields_recent_tools_in_order() -> None:
    summary = summarize_runtime_snapshot(_payload({'execution_trace_summary': _SUMMARY_TURNS_SHUFLED}), list_limit=3)

    names = [item['tool_name'] for item in (summary or {}).get('recent_tool_steps') or []]
    assert names == ['oldest', 'middle', 'newest'], '摘要的 turns 存量行不保证有序，必须按轮次 seq 排'


def test_full_trace_body_still_takes_precedence() -> None:
    payload = _payload({
        'execution_trace_summary': _SUMMARY_TURNS_SHUFLED,
        'execution_trace': {'tool_steps': [{'tool_name': 'from_trace', 'status': 'success', 'output_text': 'z'}]},
    })

    summary = summarize_runtime_snapshot(payload, list_limit=3)

    names = [item['tool_name'] for item in (summary or {}).get('recent_tool_steps') or []]
    assert names == ['from_trace']


def test_last_n_window_keeps_the_newest_rounds_not_the_first_listed() -> None:
    summary = summarize_runtime_snapshot(_payload({'execution_trace_summary': _SUMMARY_TURNS_SHUFLED}), list_limit=1)

    names = [item['tool_name'] for item in (summary or {}).get('recent_tool_steps') or []]
    assert names == ['newest'], '取"最近 N 条"要在按 seq 排完之后取尾，不能取存下来的最后一条'
