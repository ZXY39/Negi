from __future__ import annotations

import json

from main.runtime.node_prompt_contract import (
    NODE_DYNAMIC_CONTRACT_KIND,
    NodeRuntimeToolContract,
    extract_node_dynamic_contract_payload,
    inject_node_dynamic_contract_message,
    upsert_node_dynamic_contract_message,
)


def test_upsert_node_dynamic_contract_message_replaces_existing_contract_message() -> None:
    base_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": json.dumps({"prompt": "stable bootstrap"}, ensure_ascii=False)},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "message_type": NODE_DYNAMIC_CONTRACT_KIND,
                    "callable_tool_names": ["exec"],
                    "candidate_tools": ["filesystem_write"],
                },
                ensure_ascii=False,
            ),
        },
    ]
    contract = NodeRuntimeToolContract(
        node_id="node:test",
        node_kind="execution",
        callable_tool_names=["filesystem_write"],
        candidate_tool_names=[],
        candidate_tool_items=[],
        visible_skills=[{"skill_id": "memory", "display_name": "memory", "description": ""}],
        candidate_skill_ids=["memory"],
        candidate_skill_items=[{"skill_id": "memory", "description": "memory help"}],
        stage_payload={"goal": "write file"},
        hydrated_executor_names=["filesystem_write"],
        lightweight_tool_ids=["filesystem"],
        selection_trace={"mode": "execution_tool_selection"},
    )

    updated = upsert_node_dynamic_contract_message(base_messages, contract)

    assert len(updated) == 3
    assert updated[-1]["role"] == "assistant"
    assert updated[-1]["content"].startswith("## Runtime Tool Contract")
    assert '"message_type"' not in updated[-1]["content"]
    payload = contract.to_message_payload()
    assert payload["message_type"] == NODE_DYNAMIC_CONTRACT_KIND
    assert payload["callable_tool_names"] == ["filesystem_write"]
    assert payload["candidate_tools"] == []
    assert payload["candidate_skills"] == [{"skill_id": "memory", "description": "memory help"}]
    assert payload["hydrated_executor_names"] == ["filesystem_write"]
    assert "visible_skills" not in payload
    assert "lightweight_tool_ids" not in payload
    assert "model_visible_tool_selection_trace" not in payload
    assert "node_id" not in payload
    assert "node_kind" not in payload


def test_node_runtime_contract_serializes_minimal_agent_facing_payload() -> None:
    contract = NodeRuntimeToolContract(
        node_id="node:test",
        node_kind="execution",
        callable_tool_names=["exec"],
        candidate_tool_names=["filesystem_write"],
        candidate_tool_items=[{"tool_id": "filesystem_write", "description": ""}],
        visible_skills=[{"skill_id": "tmux", "display_name": "tmux", "description": "terminal workflow"}],
        candidate_skill_ids=["tmux"],
        candidate_skill_items=[{"skill_id": "tmux", "description": "terminal workflow"}],
        stage_payload={},
        hydrated_executor_names=["filesystem_write"],
        lightweight_tool_ids=["filesystem"],
        selection_trace={"mode": "execution_tool_selection"},
    )

    payload = contract.to_message_payload()
    message = contract.to_message()

    assert payload["candidate_tools"] == [
        {
            "tool_id": "filesystem_write",
            "description": "",
        }
    ]
    assert payload["candidate_skills"] == [
        {
            "skill_id": "tmux",
            "description": "terminal workflow",
        }
    ]
    assert payload["hydrated_executor_names"] == ["filesystem_write"]
    assert "visible_skills" not in payload
    assert "lightweight_tool_ids" not in payload
    assert "model_visible_tool_selection_trace" not in payload
    assert "node_id" not in payload
    assert "node_kind" not in payload
    assert message["role"] == "assistant"
    assert message["content"].startswith("## Runtime Tool Contract")
    assert '"message_type"' not in message["content"]
    assert "callable_tools: `exec`" in message["content"]
    assert "candidate_tools:" in message["content"]
    assert 'load_tool_context(tool_id="filesystem_write")' in message["content"]
    assert "hydrated_tools: `filesystem_write`" in message["content"]


def test_inject_node_dynamic_contract_message_appends_contract_to_request_tail() -> None:
    contract = NodeRuntimeToolContract(
        node_id="node-1",
        node_kind="execution",
        callable_tool_names=["submit_next_stage"],
        candidate_tool_names=["filesystem_write"],
        candidate_tool_items=[{"tool_id": "filesystem_write", "description": "write file"}],
        visible_skills=[],
        candidate_skill_ids=["tmux"],
        candidate_skill_items=[{"skill_id": "tmux", "description": "terminal workflow"}],
        stage_payload={
            "has_active_stage": True,
            "transition_required": False,
            "active_stage": {
                "stage_id": "stage-1",
                "stage_goal": "inspect repo",
                "tool_round_budget": 6,
            },
        },
        hydrated_executor_names=[],
        lightweight_tool_ids=[],
        selection_trace={"mode": "execution_tool_selection"},
    )

    injected = inject_node_dynamic_contract_message(
        [
            {"role": "system", "content": "node system"},
            {"role": "user", "content": '{"prompt":"inspect repo"}'},
            {"role": "assistant", "content": "prior reasoning"},
        ],
        contract,
    )

    assert [item["role"] for item in injected] == ["system", "user", "assistant", "assistant"]
    assert injected[-1]["content"].startswith("## Runtime Tool Contract")
    assert '"message_type"' not in injected[-1]["content"]
    payload = extract_node_dynamic_contract_payload(injected)
    assert payload is not None
    assert payload["candidate_skills"] == [{"skill_id": "tmux", "description": "terminal workflow"}]
    assert payload["hydrated_executor_names"] == []
