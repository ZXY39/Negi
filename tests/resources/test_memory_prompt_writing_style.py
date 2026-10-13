from __future__ import annotations

import importlib
from pathlib import Path


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _repo_file(rel: str) -> Path:
    return Path(__file__).resolve().parents[2] / rel


def test_memory_agent_prompt_states_style_by_source() -> None:
    """Both carriers the memory agent reads must state the same split.

    The prose playbook in main/prompts/memory_agent.md and the rule block appended to
    the request are edited separately; a one-sided change leaves the agent with two
    different instructions about how a memory line should read.
    """
    text = _repo_file("main/prompts/memory_agent.md").read_text(encoding="utf-8")
    assert "decision_source=user" in text
    assert "陈述句" in text
    assert "条件+要求" in text

    source = _repo_file("g3ku/agent/memory_agent_runtime.py").read_text(encoding="utf-8")
    assert "content 写法按来源分" in source


def _cfg():
    from g3ku.config.schema import MemoryToolsConfig

    return MemoryToolsConfig()


def test_built_user_prompt_carries_the_style_rule(tmp_path: Path) -> None:
    module = _module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="user",
                    payload_text="用户要求：结论放在最前面。",
                    created_at="2026-04-17T10:00:00+08:00",
                    request_id="write_style_1",
                )
            ],
        )
        prompt = manager._memory_agent_user_prompt(
            batch=batch,
            snapshot_text="---\nid:Ab12Cd\n2026/4/17-user：\n用户偏好：回复先给结论。\n",
            repair_reason="",
        )
        assert "decision_source=user 写成陈述句" in prompt
        assert "decision_source=self 写成条件+要求" in prompt
    finally:
        manager.close()
