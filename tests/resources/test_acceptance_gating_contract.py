"""验收按判据逐条判定：提示词两条件 + 工具面显式成对。

验收节点只应在「错误代价高」且「父节点收尾验不完」时出现。提示词与工具级 description
负责判据，工具契约负责让 acceptance prompt 不能单独开启一个验收节点。倾向句（"默认不
开""拿不准不建"）不在契约里：实盘被模型当作结论直接引用过一次。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from g3ku.json_schema_utils import normalize_openai_tool_definition
from g3ku.resources.description_limits import TOOL_DESCRIPTION_MAX_CHARS
from main.prompts import load_prompt
from main.runtime.internal_tools import SpawnChildNodesTool
from tools.create_async_task_cn.main.tool import _CreateAsyncTaskHandler

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTDOOR_PROMPT = REPO_ROOT / "g3ku" / "runtime" / "prompts" / "ceo_frontdoor.md"
CREATE_TASK_RESOURCE = REPO_ROOT / "tools" / "create_async_task_cn" / "resource.yaml"


def _child(**extra):
    return {
        "goal": "g",
        "prompt": "p",
        "execution_policy": {"mode": "focus"},
        **extra,
    }


def _spawn_tool() -> SpawnChildNodesTool:
    async def _spawn(specs, round_id, wait_all):
        return []

    return SpawnChildNodesTool(_spawn)


def _create_tool(service=None) -> _CreateAsyncTaskHandler:
    class _StubService:
        async def precheck_async_task_creation(self, **kwargs):
            return {
                "decision": "reject_use_append_notice",
                "matched_task_id": "task:stub",
                "reason": "stub",
            }

    return _CreateAsyncTaskHandler(service or _StubService())


def test_execution_prompt_judges_acceptance_by_two_conditions() -> None:
    prompt = load_prompt("node_execution.md")

    assert "**开不开验收按候选逐条判定**" in prompt
    assert "**错误代价高**" in prompt
    assert "**你收尾时验不完**" in prompt
    assert "汇总前核对（必做）" in prompt
    assert "「会被父节点汇总进 `answer`」「是交付物」都不构成开验收的理由" in prompt
    assert "不等于已经判过" in prompt
    # 倾向句不进契约：实盘里模型把「默认不开」当结论引用过，逐条判两条才是判据。
    assert "验收默认不开" not in prompt
    assert "拿不准时不建验收节点" not in prompt
    # 旧的宽判据不能回来：它们几乎命中所有真实分支。
    assert "产出会被上游当事实继续用" not in prompt
    assert "产出是落盘或被改写的文件，或带数量与范围阈值" not in prompt
    assert "该候选要跨多份产出或多个目录推进" not in prompt


def test_frontdoor_prompt_judges_final_acceptance_by_two_conditions() -> None:
    prompt = FRONTDOOR_PROMPT.read_text(encoding="utf-8")

    assert "开不开最终验收由下面两条判据决定" in prompt
    assert "**错误代价高**" in prompt
    assert "**你转述时验不完**" in prompt
    assert "不等于已经判过" in prompt
    # 倾向句与整类豁免都不能回来。
    assert "最终验收默认不开" not in prompt
    assert "问答、探索、一次性取值" not in prompt
    # 交付物本身不构成开最终验收的理由，也不把核对义务下放给前门。
    assert "「面向用户」「是交付物」不构成开最终验收的理由" not in prompt
    assert "由你在转述前至少核一次" not in prompt


def test_acceptance_carriers_keep_the_two_escape_routes_closed() -> None:
    """工具级那层只补两条出口：省略不等于判过、自测不等于验收。

    两条判据本身住在提示词里，不在第二处重印。资源包的工具级 description 受
    `TOOL_DESCRIPTION_MAX_CHARS` 约束（超长会被判 repair_required、资源根本不进表），
    所以细则归提示词、指针归工具级；字段级说明到不了模型（provider 侧递归删
    `description`），不能当载体。
    """
    manifest = yaml.safe_load(CREATE_TASK_RESOURCE.read_text(encoding="utf-8"))
    task_description = str(manifest.get("description") or "")
    assert len(task_description) <= TOOL_DESCRIPTION_MAX_CHARS
    assert "omitting it is not an answer" in task_description
    assert "self-test is not acceptance" in task_description

    spawn_description = str(
        normalize_openai_tool_definition(_spawn_tool().to_model_schema())["function"].get("description") or ""
    )
    assert "leaving it out is not an answer" in spawn_description
    assert "judge the two conditions in your prompt" in spawn_description


def test_spawn_child_nodes_rejects_prompt_without_flag() -> None:
    errors = _spawn_tool().validate_params(
        {"children": [_child(acceptance_prompt="核验落盘产物")]}
    )

    assert any("requires_acceptance is not true" in error for error in errors)


def test_spawn_child_nodes_rejects_flag_without_prompt() -> None:
    errors = _spawn_tool().validate_params(
        {"children": [_child(requires_acceptance=True)]}
    )

    assert any("acceptance_prompt is required" in error for error in errors)


def test_spawn_child_nodes_accepts_paired_acceptance() -> None:
    errors = _spawn_tool().validate_params(
        {"children": [_child(requires_acceptance=True, acceptance_prompt="核验落盘产物")]}
    )

    assert errors == []


def test_create_async_task_rejects_prompt_without_flag() -> None:
    errors = _create_tool().validate_params(
        {
            "task": "t",
            "core_requirement": "c",
            "execution_policy": {"mode": "focus"},
            "final_acceptance_prompt": "核验最终交付物",
        }
    )

    assert any("requires_final_acceptance is not true" in error for error in errors)


@pytest.mark.asyncio
async def test_create_async_task_execute_does_not_infer_acceptance_from_prompt() -> None:
    tool = _create_tool()

    with pytest.raises(ValueError, match="requires_final_acceptance is not true"):
        await tool.execute(
            "t",
            core_requirement="c",
            execution_policy={"mode": "focus"},
            final_acceptance_prompt="核验最终交付物",
            __g3ku_runtime={"session_key": "web:test"},
        )


@pytest.mark.asyncio
async def test_create_async_task_forwards_explicit_acceptance_flag() -> None:
    captured: dict[str, object] = {}

    class _CapturingService:
        async def precheck_async_task_creation(self, **kwargs):
            captured.update(kwargs)
            return {
                "decision": "reject_use_append_notice",
                "matched_task_id": "task:stub",
                "reason": "stub",
            }

    tool = _create_tool(_CapturingService())
    await tool.execute(
        "t",
        core_requirement="c",
        execution_policy={"mode": "focus"},
        requires_final_acceptance=True,
        final_acceptance_prompt="核验最终交付物",
        __g3ku_runtime={"session_key": "web:test"},
    )

    assert captured["requires_final_acceptance"] is True
    assert captured["final_acceptance_prompt"] == "核验最终交付物"
