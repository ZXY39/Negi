from __future__ import annotations

from g3ku.runtime.frontdoor._ceo_runtime_ops import (
    CeoFrontDoorRuntimeOps,
)


def test_compaction_tail_bound_truncates_oversized_tool_results() -> None:
    # Regression: LLM token 压缩无条件保留最近 4 条 body 消息；若其中一条是
    # 超大工具结果（例如 content_open 带出单行巨型 artifact），压缩后估算
    # 依然超窗、压缩检查必抛错，形成"每轮压缩每轮失败"的死循环。
    # 尾部工具结果必须被硬性截断到上限，保证压缩总能收敛。
    messages = [
        {"role": "tool", "tool_call_id": "call-1", "name": "content_open", "content": "x" * 400_000},
        {"role": "assistant", "content": "y" * 100},
    ]
    bounded = CeoFrontDoorRuntimeOps._bound_frontdoor_compaction_tail_messages(messages)

    assert len(bounded) == 2
    tool_message = bounded[0]
    assert tool_message["tool_call_id"] == "call-1"
    assert tool_message["name"] == "content_open"
    content = str(tool_message["content"] or "")
    assert len(content) <= CeoFrontDoorRuntimeOps._FRONTDOOR_COMPACTION_TAIL_CONTENT_CHAR_LIMIT + 400
    assert "内容已截断" in content
    assert "artifact" in content
    # 非 tool 消息原样保留
    assert bounded[1]["content"] == "y" * 100