"""压缩指令的合同：单发与分块共用同一段反续写收口，且都不进 tools[]。

实盘 2026-11-01 会话 ext:qq-official-1903529517:f8a8001865631301：第 1 块（269 条历史）
的 helper 没有输出摘要，而是顺着块尾的 assistant「我马上诊断」继续排查。当时分块形状
把整段历史打包进一个 user JSON，模型看不到"该由我回答"的那一轮，也没有那句"不要继续
上述对话"。这两条用例钉住修复后的形状与措辞，防止两条车道各自漂移。
"""

from __future__ import annotations

import g3ku.runtime.frontdoor._ceo_runtime_ops as frontdoor_ops
import main.runtime.react_loop as node_ops


def test_frontdoor_single_shot_and_chunk_instructions_share_the_anti_continuation_clause() -> None:
    guardrail = frontdoor_ops._FRONTDOOR_TOKEN_COMPRESSION_GUARDRAIL

    assert guardrail in frontdoor_ops._FRONTDOOR_TOKEN_COMPRESSION_INSTRUCTION
    chunk_text = frontdoor_ops._FRONTDOOR_TOKEN_COMPRESSION_CHUNK_INSTRUCTION_TEMPLATE.format(
        chunk_index=3,
        chunk_count=8,
    )
    assert guardrail in chunk_text
    assert "第 3/8 块" in chunk_text
    # 块指令不许谎称自己覆盖整段会话。
    assert "不要声称这份摘要覆盖整段会话" in chunk_text
    # 单发仍然声明"完整历史"，两种形状不能混成一句话。
    assert "完整历史" in frontdoor_ops._FRONTDOOR_TOKEN_COMPRESSION_INSTRUCTION
    assert "完整历史" not in chunk_text


def test_node_single_shot_and_chunk_instructions_share_the_anti_continuation_clause() -> None:
    guardrail = node_ops._NODE_TOKEN_COMPRESSION_GUARDRAIL

    single_shot = node_ops._NODE_TOKEN_COMPRESSION_INSTRUCTION_TEMPLATE.format(task_goal="目标")
    chunk_text = node_ops._NODE_TOKEN_COMPRESSION_CHUNK_INSTRUCTION_TEMPLATE.format(
        task_goal="目标",
        chunk_index=2,
        chunk_count=5,
    )

    assert guardrail in single_shot
    assert guardrail in chunk_text
    assert "第 2/5 块" in chunk_text
    assert "不要声称这份摘要覆盖整个任务" in chunk_text
    assert "完整历史" in single_shot
    assert "完整历史" not in chunk_text
    # 节点的任务目标必须留在块指令里：块摘要也要按目标取舍。
    assert "当前任务目标：目标" in chunk_text


def test_chunk_request_shape_survives_the_wire_role_invariant() -> None:
    """块请求过发送咽喉点后形状不变：只有一份首位 system，末位仍是那条 user 指令。

    线体合同是「system 至多一份且必须在首位」，中段 system 会被就地降级成 user——
    那会挪掉这条块的落点。块指令本来就用 user，这条用例钉住它不被改写。
    """
    from main.runtime.chat_backend import sanitize_provider_messages

    chunk_messages = [
        {"role": "system", "content": frontdoor_ops._FRONTDOOR_TOKEN_COMPRESSION_SYSTEM_PROMPT},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call:1"}]},
        {"role": "tool", "tool_call_id": "call:1", "name": "exec", "content": "r1"},
        {
            "role": "user",
            "content": frontdoor_ops._FRONTDOOR_TOKEN_COMPRESSION_CHUNK_INSTRUCTION_TEMPLATE.format(
                chunk_index=1,
                chunk_count=2,
            ),
        },
    ]

    sanitized = sanitize_provider_messages(chunk_messages)

    assert [str(item.get("role") or "") for item in sanitized] == [
        str(item.get("role") or "") for item in chunk_messages
    ], "块请求的角色不该被咽喉点改写"
    assert str((sanitized[-1] or {}).get("content") or "").startswith("【上下文压缩指令】")

