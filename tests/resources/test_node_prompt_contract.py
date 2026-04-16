from __future__ import annotations

import json

from main.runtime.node_prompt_contract import (
    NODE_DYNAMIC_CONTRACT_KIND,
    NodeRuntimeToolContract,
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
        visible_skills=[{"skill_id": "memory", "display_name": "memory", "description": ""}],
        candidate_skill_ids=[],
        stage_payload={"status": "进行中", "goal": "write file"},
        hydrated_executor_names=["filesystem_write"],
        lightweight_tool_ids=["filesystem"],
        selection_trace={"mode": "execution_tool_selection"},
    )

    updated = upsert_node_dynamic_contract_message(base_messages, contract)

    assert len(updated) == 3
    payload = json.loads(updated[-1]["content"])
    assert payload["message_type"] == NODE_DYNAMIC_CONTRACT_KIND
    assert payload["callable_tool_names"] == ["filesystem_write"]
    assert payload["candidate_tools"] == []
    assert payload["visible_skills"][0]["skill_id"] == "memory"


def test_node_runtime_contract_serializes_candidate_tools_as_structured_items() -> None:
    contract = NodeRuntimeToolContract(
        node_id="node:test",
        node_kind="execution",
        callable_tool_names=["exec"],
        candidate_tool_names=["filesystem_write"],
        visible_skills=[],
        candidate_skill_ids=[],
        stage_payload={},
        hydrated_executor_names=[],
        lightweight_tool_ids=[],
        selection_trace={"mode": "execution_tool_selection"},
    )

    payload = contract.to_message_payload()

    assert payload["candidate_tools"] == [
        {
            "tool_id": "filesystem_write",
            "description": "",
        }
    ]
