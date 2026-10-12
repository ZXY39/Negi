"""agent 端"最近工具调用"在快照只带摘要时仍要有读者，且必须按 (阶段序, 轮序) 排。

任务详情快照不再内联轨迹正文（1.82 MB 里 1.42 MB 那一块）之后，
`summarize_runtime_snapshot` 原来的两级读者是 `execution_trace.tool_steps` → 运行帧；
中间那级没了来源，agent 看到的最近工具会直接掉到帧上。这里补摘要读者作为第二级。
"""

from g3ku.runtime.tool_watchdog import summarize_runtime_snapshot


def _payload(root_node: dict) -> dict:
    return {
        'task': {'task_id': 'task:watchdog', 'status': 'in_progress'},
        'root_node': {'node_id': 'node:root', 'goal': 'do the thing', 'status': 'in_progress', **root_node},
        'frontier': [],
    }


_SUMMARY_STAGES_SHUFLED = {
    'stages': [
        {'stage_index': 3, 'rounds': [{'round_index': 1, 'tools': [{'tool_name': 'third', 'status': 'success', 'output_text': 'c'}]}]},
        {'stage_index': 1, 'rounds': [{'round_index': 1, 'tools': [{'tool_name': 'first', 'status': 'success', 'output_text': 'a'}]}]},
        {'stage_index': 2, 'rounds': [{'round_index': 5, 'tools': [{'tool_name': 'second', 'status': 'running', 'output_text': 'b'}]}]},
    ]
}


def test_summary_only_root_node_still_yields_recent_tools_in_order() -> None:
    summary = summarize_runtime_snapshot(_payload({'execution_trace_summary': _SUMMARY_STAGES_SHUFLED}), list_limit=3)

    names = [item['tool_name'] for item in (summary or {}).get('recent_tool_steps') or []]
    assert names == ['first', 'second', 'third'], '摘要的 stages 存量行不保证有序，必须按阶段/轮次排'


def test_full_trace_body_still_takes_precedence() -> None:
    payload = _payload({
        'execution_trace_summary': _SUMMARY_STAGES_SHUFLED,
        'execution_trace': {'tool_steps': [{'tool_name': 'from_trace', 'status': 'success', 'output_text': 'z'}]},
    })

    summary = summarize_runtime_snapshot(payload, list_limit=3)

    names = [item['tool_name'] for item in (summary or {}).get('recent_tool_steps') or []]
    assert names == ['from_trace']


def test_last_n_window_keeps_the_newest_rounds_not_the_first_listed() -> None:
    summary = summarize_runtime_snapshot(_payload({'execution_trace_summary': _SUMMARY_STAGES_SHUFLED}), list_limit=1)

    names = [item['tool_name'] for item in (summary or {}).get('recent_tool_steps') or []]
    assert names == ['third']
