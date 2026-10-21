from __future__ import annotations

import importlib
from pathlib import Path

SECTION_MARKERS = ("## 方法论", "## 经验教训", "## 用户偏好")


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _repo_file(rel: str) -> Path:
    return Path(__file__).resolve().parents[2] / rel


def _cfg():
    from g3ku.config.schema import MemoryToolsConfig

    return MemoryToolsConfig()


def test_consolidator_prompt_states_type_sections_in_both_carriers() -> None:
    """The consolidator playbook and its inline rule block must agree.

    The two carriers are edited separately; a one-sided change leaves the agent
    with two different instructions about how a category note is sectioned.
    """
    text = _repo_file("main/prompts/memory_consolidator.md").read_text(encoding="utf-8")
    for marker in SECTION_MARKERS:
        assert marker in text
    assert "判定条件" in text

    source = _repo_file("g3ku/agent/memory_agent_runtime.py").read_text(encoding="utf-8")
    assert "分类 note 正文内部按 ## 方法论 / ## 经验教训 / ## 用户偏好 分节" in source
    assert "方法论那一节每行保留判定条件" in source


def test_built_consolidation_prompt_carries_the_section_rule(tmp_path: Path) -> None:
    module = _module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        batch = module.MemoryBatch(
            op="consolidate",
            items=[
                module.MemoryQueueRequest(
                    op="consolidate",
                    decision_source="self",
                    payload_text="## 待归并记忆\n1) 高动态站点抓取时，要先用 CDP 触发真实请求。",
                    created_at="2026-10-21T10:00:00+08:00",
                    request_id="consolidate_style_1",
                )
            ],
        )
        prompt = manager._consolidation_agent_user_prompt(
            batch=batch,
            snapshot_text="2026/4/17-用户：用户偏好：回复先给结论。",
            repair_reason="",
        )
        assert "## 方法论 / ## 经验教训 / ## 用户偏好" in prompt
        assert "判定条件" in prompt
    finally:
        manager.close()
