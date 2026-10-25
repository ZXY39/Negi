from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.tool_contract_block import attach_tool_contract_block, render_tool_contract_block
from main.governance.tool_context import build_tool_context_fingerprint


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo_tool"

    @property
    def description(self) -> str:
        return "echo tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, value: str, **kwargs):
        _ = kwargs
        return {"ok": True, "value": value}


def test_graph_review_tool_calls_interrupt_payload_includes_runtime_contract_and_snapshot_fields(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_interrupt(*, state, payload):
        _ = state
        captured["payload"] = payload
        return {
            "type": "submit_batch_review",
            "batch_id": "batch:123",
            "decisions": [
                {"tool_call_id": "call-1", "decision": "approve"},
            ],
        }

    monkeypatch.setattr(ceo_runtime_ops, "raise_frontdoor_approval_interrupt", _fake_interrupt)

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    runtime = SimpleNamespace(context=SimpleNamespace(session=None))

    result = runner._graph_review_tool_calls(
        {
            "approval_request": {
                "kind": "frontdoor_tool_approval_batch",
                "batch_id": "batch:123",
                "mode": "regulatory_review",
                "submission_mode": "batch_submit_only",
                "tool_calls": [{"id": "call-1", "name": "load_tool_context", "arguments": {"tool_id": "filesystem_write"}}],
                "review_items": [
                    {
                        "tool_call_id": "call-1",
                        "name": "load_tool_context",
                        "risk_level": "high",
                        "arguments": {"tool_id": "filesystem_write"},
                    }
                ],
                "pass_through_tool_call_ids": [],
            },
            "tool_call_payloads": [
                {
                    "id": "call-1",
                    "name": "load_tool_context",
                    "arguments": {"tool_id": "filesystem_write"},
                }
            ],
            "compression_state": {"status": "running", "text": "compressing", "source": "user"},
            "semantic_context_state": {"summary_text": "summary", "needs_refresh": False},
            # 存量会话状态里残留的台账键：裁决卡片的载荷只从 approval_request 与下面几份
            # 快照拼装，不铺 `**state`，所以这些键一律不得出现在评审卡片上。
            "hydrated_tool_names": ["filesystem_write"],
            "frontdoor_selection_debug": {"tool_selection": {"candidate_tool_names": ["filesystem_write"]}},
        },
        runtime=runtime,
    )

    assert result == {
        "approval_request": None,
        "approval_status": "approved",
        "approval_batch_id": "batch:123",
        "tool_call_payloads": [
            {
                "id": "call-1",
                "name": "load_tool_context",
                "arguments": {"tool_id": "filesystem_write"},
            }
        ],
        # 全批批准：执行名单等于原名单，没有合成拒绝要合并。
        "executable_tool_call_payloads": [
            {
                "id": "call-1",
                "name": "load_tool_context",
                "arguments": {"tool_id": "filesystem_write"},
            }
        ],
        "synthetic_tool_results": [],
        "next_step": "execute_tools",
    }
    payload = dict(captured["payload"] or {})
    assert payload["kind"] == "frontdoor_tool_approval_batch"
    assert payload["batch_id"] == "batch:123"
    assert payload["tool_calls"] == [{"id": "call-1", "name": "load_tool_context", "arguments": {"tool_id": "filesystem_write"}}]
    assert payload["review_items"] == [
        {
            "tool_call_id": "call-1",
            "name": "load_tool_context",
            "risk_level": "high",
            "arguments": {"tool_id": "filesystem_write"},
        }
    ]
    assert payload["compression_state"] == {"status": "running", "text": "compressing", "source": "user"}
    assert "semantic_context_state" not in payload
    assert "hydrated_tool_names" not in payload
    assert payload["tool_call_payloads"] == [
        {
            "id": "call-1",
            "name": "load_tool_context",
            "arguments": {"tool_id": "filesystem_write"},
        }
    ]
    assert payload["frontdoor_selection_debug"] == {
        "tool_selection": {"candidate_tool_names": ["filesystem_write"]}
    }
    assert "frontdoor_stage_state" not in payload
    assert "canonical_context" not in payload


def test_graph_review_tool_calls_rejects_partial_batch_submit(monkeypatch) -> None:
    def _fake_interrupt(*, state, payload):
        _ = state, payload
        return {
            "type": "submit_batch_review",
            "batch_id": "batch:123",
            "decisions": [
                {"tool_call_id": "call-1", "decision": "approve"},
            ],
        }

    monkeypatch.setattr(ceo_runtime_ops, "raise_frontdoor_approval_interrupt", _fake_interrupt)

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    runtime = SimpleNamespace(context=SimpleNamespace(session=None))

    with pytest.raises(ValueError, match="cover every review item exactly once"):
        runner._graph_review_tool_calls(
            {
                "approval_request": {
                    "kind": "frontdoor_tool_approval_batch",
                    "batch_id": "batch:123",
                    "mode": "regulatory_review",
                    "submission_mode": "batch_submit_only",
                    "tool_calls": [
                        {"id": "call-1", "name": "exec", "arguments": {"command": "echo hi"}},
                        {"id": "call-2", "name": "filesystem_edit", "arguments": {"path": "/tmp/a"}},
                    ],
                    "review_items": [
                        {
                            "tool_call_id": "call-1",
                            "name": "exec",
                            "risk_level": "high",
                            "arguments": {"command": "echo hi"},
                        },
                        {
                            "tool_call_id": "call-2",
                            "name": "filesystem_edit",
                            "risk_level": "high",
                            "arguments": {"path": "/tmp/a"},
                        },
                    ],
                    "pass_through_tool_call_ids": [],
                },
                "tool_call_payloads": [
                    {"id": "call-1", "name": "exec", "arguments": {"command": "echo hi"}},
                    {"id": "call-2", "name": "filesystem_edit", "arguments": {"path": "/tmp/a"}},
                ],
            },
            runtime=runtime,
        )


def test_build_synthetic_rejection_result_includes_optional_note() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = runner._build_synthetic_rejection_result(
        tool_call_id="call-2",
        tool_name="exec",
        note="请先给出无需 exec 的方案。",
    )

    assert result["tool_call_id"] == "call-2"
    assert result["tool_name"] == "exec"
    assert result["status"] == "error"
    assert "用户不允许运行此工具" in result["result_text"]
    assert "补充说明：请先给出无需 exec 的方案。" in result["result_text"]
    assert result["tool_message"]["role"] == "tool"
    assert result["tool_message"]["tool_call_id"] == "call-2"


def test_merge_ordered_tool_results_preserves_original_tool_call_order() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    original_payloads = [
        {"id": "call-1", "name": "content_open", "arguments": {"path": "/tmp/a"}},
        {"id": "call-2", "name": "exec", "arguments": {"command": "echo hi"}},
        {"id": "call-3", "name": "agent_browser", "arguments": {"url": "https://example.com"}},
    ]
    real_results = [
        {
            "tool_call_id": "call-1",
            "tool_name": "content_open",
            "status": "success",
            "result_text": "opened",
            "tool_message": {"role": "tool", "tool_call_id": "call-1", "name": "content_open", "content": "opened"},
        },
        {
            "tool_call_id": "call-3",
            "tool_name": "agent_browser",
            "status": "success",
            "result_text": "browsed",
            "tool_message": {"role": "tool", "tool_call_id": "call-3", "name": "agent_browser", "content": "browsed"},
        },
    ]
    synthetic_results = [
        runner._build_synthetic_rejection_result(
            tool_call_id="call-2",
            tool_name="exec",
            note="请先给出无需 exec 的方案。",
        )
    ]

    merged = runner._merge_ordered_tool_results(
        original_payloads=original_payloads,
        real_results=real_results,
        synthetic_results=synthetic_results,
    )

    assert [item["tool_call_id"] for item in merged] == ["call-1", "call-2", "call-3"]
    assert [item["tool_name"] for item in merged] == ["content_open", "exec", "agent_browser"]


def _contract_load_row(tool_id: str, *, tool_call_id: str = "call-load-1") -> dict:
    """一条**真在场**的 load 行：JSON 信封的 `content` 里装着 `[TOOL_CONTRACT:…]` 块。

    只带 `tool_context_fingerprint` 键而没有块，在场判据读成"不在场"，用例会因此假绿。
    """
    fingerprint = "tcf:" + ((tool_id or "tool") * 20)[:16]
    return {
        "role": "tool",
        "name": "load_tool_context",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "ok": True,
                "tool_id": tool_id,
                "tool_context_fingerprint": fingerprint,
                "content": render_tool_contract_block(tool_id, fingerprint, f"# {tool_id}\n契约正文"),
            },
            ensure_ascii=False,
        ),
    }


def test_frontdoor_tool_state_after_tool_results_reads_presence_not_load_payload_targets() -> None:
    """常驻内置没有 toolskill，零契约块也留在 callable；带 toolskill 的名字只由块在场决定。

    `load` 回执里的 `hydration_targets` 不记账：同一份状态转真的唯一入口是请求视图里
    的那一行契约块。
    """
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    base_state = {
        "tool_names": ["load_tool_context", "exec"],
        "candidate_tool_names": ["agent_browser"],
        "rbac_visible_tool_names": ["load_tool_context", "exec", "agent_browser"],
        "messages": [],
    }

    result = runner._frontdoor_tool_state_after_tool_results(state=dict(base_state))

    assert result == {
        "tool_names": ["load_tool_context", "exec", "silent"],
        "candidate_tool_names": ["agent_browser"],
        "candidate_tool_items": [{"tool_id": "agent_browser", "description": ""}],
    }

    # 契约块进了请求视图：同一个名字当跳从候选转进 callable，不需要任何一步"提升"记账。
    hop_state = {**base_state, "messages": [_contract_load_row("agent_browser")]}
    promoted = runner._frontdoor_tool_state_after_tool_results(state=hop_state)
    assert "agent_browser" in promoted["tool_names"]
    assert promoted["candidate_tool_names"] == []
    assert promoted["candidate_tool_items"] == []


def test_frontdoor_tool_state_promotes_mid_turn_registered_tool_by_contract_presence() -> None:
    """回合中途注册的工具：候选里没有它（回合初快照没这个名），load 把块送进请求视图后
    就当跳可调——提升不再信 load 回执的 `hydration_targets`，也不再要 candidate 命中。"""

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    base_state = {
        "tool_names": ["load_tool_context", "windows_mcp"],
        "candidate_tool_names": [],
        "rbac_visible_tool_names": ["load_tool_context", "windows_mcp"],
        "messages": [],
    }

    # 没有块 ⇒ 不提升：候选视图把它留在原地，模型仍有一条 load 的出口可达。
    absent = runner._frontdoor_tool_state_after_tool_results(state=dict(base_state))
    assert "windows_mcp" not in absent["tool_names"]
    assert absent["candidate_tool_names"] == ["windows_mcp"]

    hop_state = {**base_state, "messages": [_contract_load_row("windows_mcp")]}
    result = runner._frontdoor_tool_state_after_tool_results(state=hop_state)

    assert "windows_mcp" in result["tool_names"]
    assert "windows_mcp" not in result["candidate_tool_names"]
    # 台账键不再由工具态写回产出：写侧一旦复活，在场判据就有了第二份账。
    for legacy_key in ("hydrated_tool_names", "hydration_revoked_executor_names"):
        assert legacy_key not in result


@pytest.mark.asyncio
async def test_frontdoor_duplicate_load_tool_context_guard_blocks_same_uncompressed_toolskill() -> None:
    current_payload = {
        "ok": True,
        "tool_id": "exec",
        "content": "# exec",
        "parameter_contract_markdown": "## Parameter Contract",
        "required_parameters": ["command"],
        "example_arguments": {"command": "pwd"},
        "warnings": [],
        "errors": [],
        "callable": True,
        "available": True,
        "repair_required": False,
        "callable_now": True,
        "will_be_hydrated_next_turn": False,
        "hydration_targets": [],
        "exec_runtime_policy": {
            "mode": "governed",
            "guardrails_enabled": True,
            "summary": "exec will execute shell commands with exec-side guardrails.",
        },
    }
    current_payload["tool_context_fingerprint"] = build_tool_context_fingerprint(current_payload)
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(
                load_tool_context_v2=lambda **kwargs: dict(current_payload),
            )
        )
    )

    error_text = await runner._frontdoor_load_tool_context_duplicate_error(
        payload={"id": "call-1", "name": "load_tool_context", "arguments": {"tool_id": "exec"}},
        state={
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call-old",
                    "name": "load_tool_context",
                    # 交付形态与真车道同形：`content` 里装着契约块，判据只认它。
                    "content": json.dumps(
                        attach_tool_contract_block(current_payload), ensure_ascii=False
                    ),
                }
            ],
            "tool_names": ["load_tool_context", "exec"],
            "candidate_tool_names": [],
            "rbac_visible_tool_names": ["load_tool_context", "exec"],
        },
        runtime_context={"session_key": "web:shared", "actor_role": "ceo"},
    )

    assert error_text == (
        "Error: 上下文中已有该工具当前版本的未压缩 toolskill，禁止重复读取！"
        "请直接复用已有说明，或在工具状态变化/旧内容被压缩后再重试。"
    )


@pytest.mark.asyncio
async def test_frontdoor_duplicate_load_tool_context_guard_allows_refresh_when_fingerprint_changes() -> None:
    current_payload = {
        "ok": True,
        "tool_id": "exec",
        "content": "# exec",
        "parameter_contract_markdown": "## Parameter Contract",
        "required_parameters": ["command"],
        "example_arguments": {"command": "pwd"},
        "warnings": [],
        "errors": [],
        "callable": True,
        "available": True,
        "repair_required": False,
        "callable_now": True,
        "will_be_hydrated_next_turn": False,
        "hydration_targets": [],
        "exec_runtime_policy": {
            "mode": "full_access",
            "guardrails_enabled": False,
            "summary": "exec will execute shell commands without exec-side guardrails.",
        },
    }
    current_payload["tool_context_fingerprint"] = build_tool_context_fingerprint(current_payload)
    prior_payload = {
        "ok": True,
        "tool_id": "exec",
        "content": "# exec",
        "parameter_contract_markdown": "## Parameter Contract",
        "required_parameters": ["command"],
        "example_arguments": {"command": "pwd"},
        "warnings": [],
        "errors": [],
        "callable": True,
        "available": True,
        "repair_required": False,
        "callable_now": True,
        "will_be_hydrated_next_turn": False,
        "hydration_targets": [],
        "exec_runtime_policy": {
            "mode": "governed",
            "guardrails_enabled": True,
            "summary": "exec will execute shell commands with exec-side guardrails.",
        },
    }
    prior_payload["tool_context_fingerprint"] = build_tool_context_fingerprint(prior_payload)
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(
                load_tool_context_v2=lambda **kwargs: dict(current_payload),
            )
        )
    )

    error_text = await runner._frontdoor_load_tool_context_duplicate_error(
        payload={"id": "call-1", "name": "load_tool_context", "arguments": {"tool_id": "exec"}},
        state={
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call-old",
                    "name": "load_tool_context",
                    "content": json.dumps(prior_payload, ensure_ascii=False),
                }
            ],
            "tool_names": ["load_tool_context", "exec"],
            "candidate_tool_names": [],
            "rbac_visible_tool_names": ["load_tool_context", "exec"],
        },
        runtime_context={"session_key": "web:shared", "actor_role": "ceo"},
    )

    assert error_text == ""






@pytest.mark.asyncio
async def test_graph_execute_tools_marks_direct_contract_delivery(
    monkeypatch,
) -> None:
    """契约不在场的直接调用：实时帧带 `contract_delivery`，阶段账本同样带，工具不执行。"""
    session = SimpleNamespace(session_key="web:shared", _frontdoor_tool_arg_stash={})
    delivered: list[dict[str, object]] = []
    progress_events: list[dict[str, object]] = []

    def _deliver_impulse_contract(**kwargs):
        delivered.append(dict(kwargs))
        tool_id = str(kwargs.get("tool_id") or "")
        block = render_tool_contract_block(tool_id, "tcf:delivery", "contract body")
        return {
            "outcome": "staged",
            "content": f'{block}\n\nTool contract for "{tool_id}" loaded - the tool is now callable.',
            "carrier": {},
            "staged_count": 1,
            "executed": False,
            "fingerprint": "tcf:delivery",
        }

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=SimpleNamespace(deliver_impulse_contract=_deliver_impulse_contract))
    )

    def _registered_tools_for_state(state):
        _ = state
        return {}

    async def _on_progress(content, *, tool_hint=False, event_kind=None, event_data=None):
        _ = content, tool_hint
        progress_events.append({"kind": event_kind, "data": dict(event_data or {})})

    monkeypatch.setattr(runner, "_registered_tools_for_state", _registered_tools_for_state)
    monkeypatch.setattr(
        runner,
        "_build_tool_runtime_context",
        lambda **kwargs: {
            "on_progress": _on_progress,
            "runtime_session": session,
            "session_key": "web:shared",
            "actor_role": "ceo",
        },
    )

    async def _must_not_execute(**kwargs):
        _ = kwargs
        raise AssertionError("a contract delivery must not execute the tool")

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _must_not_execute)

    result = await runner._graph_execute_tools(
        {
            "messages": [],
            "tool_names": ["filesystem_edit"],
            "candidate_tool_names": ["filesystem_edit"],
            "visible_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": ["filesystem_edit"],
            "rbac_visible_skill_ids": [],
            "tool_call_payloads": [
                {"id": "call-delivery-1", "name": "filesystem_edit", "arguments": {"path": "/a"}}
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": False,
            "max_parallel_tool_calls": 1,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
            "session_key": "web:shared",
        },
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    assert len(delivered) == 1
    tool_message = next(
        dict(message)
        for message in list(result["messages"])
        if str(message.get("role") or "").strip().lower() == "tool"
    )
    assert "TOOL_CONTRACT:filesystem_edit:tcf:delivery" in str(tool_message.get("content") or "")
    # 契约投递也要进回合账本，刷新后会话轨才知道这里发生过一次加载。
    ledger_tool = result["frontdoor_turns"][-1]["tools"][0]
    assert ledger_tool["tool_name"] == "filesystem_edit"
    assert ledger_tool["contract_delivery"] == "staged"


    delivery_events = [
        event for event in progress_events
        if str(event["data"].get("contract_delivery") or "").strip()
    ]
    assert len(delivery_events) == 1
    assert delivery_events[0]["kind"] == "tool_result"
    assert delivery_events[0]["data"]["contract_delivery"] == "staged"




def _grafting_orphan_round() -> dict[str, object]:
    return {
        "round_id": "orphan-round-1",
        "round_index": 1,
        "created_at": "2026-04-15T00:00:00+08:00",
        "text": "补读实现",
        "tool_names": ["echo_tool"],
        "tool_call_ids": ["call-prev-1"],
        "budget_counted": True,
        "orphan": True,
    }






def _near_exhaustion_stage_state() -> dict[str, object]:
    """frontdoor-stage-1: 预算 2 已用 1，本轮就是最后一条合法轮。"""
    return {
        "stages": [
            {
                "preamble_text": "",
                "status": "active",
                "mode": "自主执行",
                "final_stage": False,
                "rounds": [
                    {
                        "round_id": "frontdoor-stage-1:round-1",
                        "round_index": 1,
                        "created_at": "2026-04-15T00:00:00+08:00",
                        "text": "",
                        "tool_names": ["echo_tool"],
                        "tool_call_ids": ["call-prev-1"],
                        "budget_counted": True,
                    }
                ],
            }
        ],
        "pending_orphan_rounds": [],
    }


def _execute_tools_state(
    *,
    tool_call_payloads: list[dict[str, object]],
    parallel_enabled: bool = False,
    max_parallel_tool_calls: int = 1,
) -> dict[str, object]:
    return {
        "messages": [],
        "tool_names": ["echo_tool"],
        "candidate_tool_names": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": ["echo_tool"],
        "rbac_visible_skill_ids": [],
        "tool_call_payloads": tool_call_payloads,
        "used_tools": [],
        "route_kind": "direct_reply",
        "parallel_enabled": parallel_enabled,
        "max_parallel_tool_calls": max_parallel_tool_calls,
        "synthetic_tool_calls_used": False,
        "response_payload": {"content": "", "tool_calls": []},
        "session_key": "web:shared",
    }












@pytest.mark.asyncio
async def test_graph_execute_tools_executes_only_approved_calls_and_merges_synthetic_rejections(
    monkeypatch,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed: list[str] = []

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"echo_tool": _EchoTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, runtime_context, on_progress, tool_call_id
        executed.append(tool_name)
        raw_result = await tool.execute(**arguments)
        return (
            raw_result,
            json.dumps(raw_result, ensure_ascii=False),
            "success",
            "2026-04-21T00:00:00+08:00",
            "2026-04-21T00:00:01+08:00",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)

    synthetic = runner._build_synthetic_rejection_result(
        tool_call_id="call-2",
        tool_name="exec",
        note="请先给出无需 exec 的方案。",
    )

    result = await runner._graph_execute_tools(
        {
            "messages": [],
            "tool_names": ["echo_tool"],
            "candidate_tool_names": [],
            "visible_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": ["echo_tool"],
            "rbac_visible_skill_ids": [],
            "tool_call_payloads": [
                {"id": "call-1", "name": "echo_tool", "arguments": {"value": "alpha"}},
                {"id": "call-2", "name": "exec", "arguments": {"command": "echo hi"}},
            ],
            "executable_tool_call_payloads": [
                {"id": "call-1", "name": "echo_tool", "arguments": {"value": "alpha"}},
            ],
            "synthetic_tool_results": [synthetic],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": True,
            "max_parallel_tool_calls": 2,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
            "session_key": "web:shared",
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    tool_messages = [
        dict(message)
        for message in list(result["messages"])
        if str(message.get("role") or "").strip().lower() == "tool"
    ]
    assert executed == ["echo_tool"]
    assert [item["tool_call_id"] for item in tool_messages] == ["call-1", "call-2"]
    assert tool_messages[0]["name"] == "echo_tool"
    assert tool_messages[1]["name"] == "exec"
    assert "用户不允许运行此工具" in str(tool_messages[1]["content"] or "")


_BATCH_PAYLOADS = [
    {"id": "call-1", "name": "content_open", "arguments": {"path": "/tmp/a"}},
    {"id": "call-2", "name": "exec", "arguments": {"command": "echo hi"}},
    {"id": "call-3", "name": "echo_tool", "arguments": {"value": "alpha"}},
]


def _batch_approval_request() -> dict:
    """两条要审（call-1/call-2）+ 一条免审（call-3），批审只需覆盖待审条目。"""
    return {
        "kind": "frontdoor_tool_approval_batch",
        "batch_id": "batch:123",
        "mode": "regulatory_review",
        "submission_mode": "batch_submit_only",
        "tool_calls": [dict(item) for item in _BATCH_PAYLOADS],
        "review_items": [
            {"tool_call_id": "call-1", "name": "content_open", "risk_level": "high", "arguments": {}},
            {"tool_call_id": "call-2", "name": "exec", "risk_level": "high", "arguments": {}},
        ],
        "pass_through_tool_call_ids": ["call-3"],
    }


def _submit_batch(decisions: list[dict]):
    def _fake_interrupt(*, state, payload):
        _ = state, payload
        return {"type": "submit_batch_review", "batch_id": "batch:123", "decisions": decisions}

    return _fake_interrupt


def _review_batch(monkeypatch, decisions: list[dict]) -> dict:
    monkeypatch.setattr(ceo_runtime_ops, "raise_frontdoor_approval_interrupt", _submit_batch(decisions))
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    reviewed = runner._graph_review_tool_calls(
        {
            "approval_request": _batch_approval_request(),
            "tool_call_payloads": [dict(item) for item in _BATCH_PAYLOADS],
        },
        runtime=SimpleNamespace(context=SimpleNamespace(session=None)),
    )
    return reviewed


def test_batch_review_rejected_item_leaves_the_executable_list(monkeypatch) -> None:
    """逐条裁决：被拒的那条不进执行名单并留下合成拒绝，批准与免审的照常执行。"""
    reviewed = _review_batch(
        monkeypatch,
        [
            {"tool_call_id": "call-1", "decision": "approve"},
            {"tool_call_id": "call-2", "decision": "reject", "note": "先别动这台机器"},
        ],
    )

    assert reviewed["next_step"] == "execute_tools"
    assert [item["id"] for item in reviewed["tool_call_payloads"]] == ["call-1", "call-2", "call-3"]
    assert [item["id"] for item in reviewed["executable_tool_call_payloads"]] == ["call-1", "call-3"]
    rejected = reviewed["synthetic_tool_results"]
    assert [item["tool_call_id"] for item in rejected] == ["call-2"]
    assert "用户不允许运行此工具" in str(rejected[0]["result_text"] or "")
    assert "补充说明：先别动这台机器" in str(rejected[0]["result_text"] or "")


def test_batch_review_all_approve_keeps_every_call_executable(monkeypatch) -> None:
    """全批批准时不产生任何合成结果，执行名单与旧合同一致。"""
    reviewed = _review_batch(
        monkeypatch,
        [
            {"tool_call_id": "call-1", "decision": "approve"},
            {"tool_call_id": "call-2", "decision": "approve"},
        ],
    )

    assert reviewed["synthetic_tool_results"] == []
    assert [item["id"] for item in reviewed["executable_tool_call_payloads"]] == ["call-1", "call-2", "call-3"]


def test_approval_request_flags_channel_sessions_only_for_the_channel_lane() -> None:
    """批次上的渠道标记按会话键判：web 会话照常进审批，ext 会话被标成无人可批。"""
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    runner._reviewable_tool_risk_map = lambda *, session_key: {"exec": "high"}
    payloads = [{"id": "call-2", "name": "exec", "arguments": {"command": "echo hi"}}]

    channel = runner._approval_request_for_tool_calls(payloads, session_key="ext:qq-official:abc")
    web = runner._approval_request_for_tool_calls(payloads, session_key="web:shared")

    assert bool(channel.get("channel_session")) is True
    assert bool(web.get("channel_session")) is False


def test_channel_session_batch_review_bounces_without_interrupt(monkeypatch) -> None:
    """渠道会话没有审批界面：要审的那几条当跳就打回，免审的那条照常执行，且不落中断。"""

    def _fail_interrupt(*, state, payload):
        _ = state, payload
        raise AssertionError("channel session must not raise an approval interrupt")

    monkeypatch.setattr(ceo_runtime_ops, "raise_frontdoor_approval_interrupt", _fail_interrupt)
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    request = _batch_approval_request()
    request["channel_session"] = True

    reviewed = runner._graph_review_tool_calls(
        {
            "approval_request": request,
            "tool_call_payloads": [dict(item) for item in _BATCH_PAYLOADS],
        },
        runtime=SimpleNamespace(context=SimpleNamespace(session=None)),
    )

    assert reviewed["next_step"] == "execute_tools"
    assert reviewed["approval_status"] == "channel_auto_rejected"
    # 整批仍按原序留三份，合并回消息时不会出现缺号的工具回执。
    assert [item["id"] for item in reviewed["tool_call_payloads"]] == ["call-1", "call-2", "call-3"]
    assert [item["id"] for item in reviewed["executable_tool_call_payloads"]] == ["call-3"]
    bounced = reviewed["synthetic_tool_results"]
    assert [item["tool_call_id"] for item in bounced] == ["call-1", "call-2"]
    assert "intercepted by risk review" in str(bounced[0]["result_text"] or "")
    # 这条回执面向模型，不写给界面上的人看。
    assert not any("\u4e00" <= char <= "\u9fff" for char in str(bounced[0]["result_text"] or ""))


@pytest.mark.asyncio
async def test_rejected_batch_item_never_reaches_the_tool_executor(monkeypatch) -> None:
    """从车道进口喂到执行：拒掉的 call-2 不进执行器，其余两条真跑，工具行按原顺序齐三份。"""
    reviewed = _review_batch(
        monkeypatch,
        [
            {"tool_call_id": "call-1", "decision": "approve"},
            {"tool_call_id": "call-2", "decision": "reject"},
        ],
    )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    executed_ids: list[str] = []
    echo_tool = _EchoTool()
    monkeypatch.setattr(
        runner,
        "_registered_tools_for_state",
        lambda state: {"content_open": echo_tool, "exec": echo_tool, "echo_tool": echo_tool},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    async def _fake_execute(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, arguments, runtime_context, on_progress
        executed_ids.append(str(tool_call_id or ""))
        raw_result = {"echoed": tool_name}
        return (
            raw_result,
            json.dumps(raw_result, ensure_ascii=False),
            "success",
            "2026-10-18T00:00:00+08:00",
            "2026-10-18T00:00:01+08:00",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute)

    state = {
        **reviewed,
        "messages": [],
        "tool_names": ["content_open", "exec", "echo_tool"],
        "candidate_tool_names": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": ["content_open", "exec", "echo_tool"],
        "rbac_visible_skill_ids": [],
        "used_tools": [],
        "route_kind": "direct_reply",
        "parallel_enabled": True,
        "max_parallel_tool_calls": 3,
        "synthetic_tool_calls_used": False,
        "response_payload": {"content": "", "tool_calls": []},
        "session_key": "web:shared",
    }
    result = await runner._graph_execute_tools(state, runtime=SimpleNamespace(context=SimpleNamespace(session=None)))

    assert executed_ids == ["call-1", "call-3"]
    tool_messages = [
        dict(message)
        for message in list(result["messages"])
        if str(message.get("role") or "").strip().lower() == "tool"
    ]
    assert [item["tool_call_id"] for item in tool_messages] == ["call-1", "call-2", "call-3"]
    assert "用户不允许运行此工具" in str(tool_messages[1]["content"] or "")


@pytest.mark.asyncio
async def test_node_prepare_turn_seeds_the_session_turn_ledger(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    async def _fake_graph_prepare(state, runtime=None):
        _ = state, runtime
        return {"messages": [{"role": "user", "content": "hi"}]}

    synced: dict[str, object] = {}

    async def _fake_emit(runtime=None):
        _ = runtime

    monkeypatch.setattr(runner, "_graph_prepare_turn", _fake_graph_prepare)
    monkeypatch.setattr(
        runner,
        "_sync_runtime_session_frontdoor_state",
        lambda *, state, runtime=None: synced.update(state),
    )
    monkeypatch.setattr(runner, "_emit_frontdoor_turns_sync_event", _fake_emit)

    ledger = [{"seq": 1, "turn_id": "turn-1", "text": "prior", "tools": []}]
    session = SimpleNamespace(
        _frontdoor_turns=[dict(item) for item in ledger],
        _current_turn_id=lambda: "turn-2",
    )
    runtime = SimpleNamespace(context=SimpleNamespace(session=session))

    update = await runner._node_prepare_turn({}, runtime)

    # 回合状态初值里账本是空的；起手不灌回去，下一次 sync 就会把会话账本清空。
    assert update["frontdoor_turns"][0]["turn_id"] == "turn-1"
    assert update["frontdoor_turn_id"] == "turn-2"
    assert synced["frontdoor_turns"][0]["turn_id"] == "turn-1"
