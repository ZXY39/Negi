"""Stage 7：提示词按"契约在场即能力"重写，五条全局规则只有一份、两条车道都收到。

计划 §4 Stage 7 的三条验证都在这里，另加两条这一阶段特有的形状约束：

- 提示词里不再出现 `callable_tools` / hydration / keep_* 这些已经不存在的机制；留着
  它们等于教模型去找一行运行时再也不渲染的东西。
- 五条规则的唯一源文件是 `main/prompts/shared_tool_reuse_rules.md`，节点道用 `{{> }}`
  include、前门用同一个展开器。抄第二份的话，改一条规则要记得改两处，而第二处没有任何
  东西会报警。
- 冲动调用的首次回执必须把每一份暂存目标列出来，只有数量不算数——批量语义的唯一安全阀。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime import tool_arg_stash
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder
from g3ku.runtime.tool_impulse import impulse_receipt, render_staged_targets
from main.prompts import load_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_PROMPT_DIR = REPO_ROOT / "main" / "prompts"
FRONTDOOR_PROMPT_DIR = REPO_ROOT / "g3ku" / "runtime" / "prompts"

RULE_SENTENCES = (
    "Authorized tools can be called directly.",
    "To re-run arguments staged by your previous call to a tool",
    "executes ALL argument sets staged for that tool in the current window",
    "To execute only some of them, do not use `reuse_args`",
    "it returns the contract without validating or staging anything",
)

# 这些机制在本轮改造里被整体删除（台账、keep 通道、callable 渲染行）。提示词提到任何一条，
# 模型都会去找一个运行时不再提供的东西。
BANNED_MECHANISM_TERMS = (
    "callable_tools",
    "hydrated_tools",
    "hydration",
    "hydrated",
    "hydrate",
    "keep_tools",
    "keep_skills",
    "kept_tool_contexts",
    "kept_skill_contexts",
    "水合",
)


def _prompt_files() -> list[Path]:
    files: list[Path] = []
    for directory in (NODE_PROMPT_DIR, FRONTDOOR_PROMPT_DIR):
        files.extend(sorted(directory.glob("*.md")))
    return files


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda path: path.name)
def test_prompts_never_reference_a_deleted_mechanism(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    offenders = [term for term in BANNED_MECHANISM_TERMS if term in text]
    assert offenders == []


def test_reuse_rules_exist_in_exactly_one_prompt_file() -> None:
    carriers = [
        path.name
        for path in _prompt_files()
        if RULE_SENTENCES[0] in path.read_text(encoding="utf-8")
    ]
    assert carriers == ["shared_tool_reuse_rules.md"]


@pytest.mark.parametrize(
    "rendered",
    [
        load_prompt("node_execution.md"),
        load_prompt("acceptance_execution.md"),
        CeoPromptBuilder(loop=SimpleNamespace(workspace=None))._read_prompt("ceo_frontdoor.md"),
    ],
    ids=["node", "acceptance", "frontdoor"],
)
def test_every_lane_receives_all_five_rules(rendered: str) -> None:
    for sentence in RULE_SENTENCES:
        assert sentence in rendered, f"missing rule: {sentence}"
    assert "{{>" not in rendered


def test_both_lanes_receive_the_rules_byte_for_byte() -> None:
    rules = load_prompt("shared_tool_reuse_rules.md").strip()
    frontdoor = CeoPromptBuilder(loop=SimpleNamespace(workspace=None))._read_prompt("ceo_frontdoor.md")
    assert rules in frontdoor
    assert rules in load_prompt("node_execution.md")


def test_presence_wording_replaces_the_callable_line() -> None:
    """两条车道都要说清"契约不在场不拦路、那一跳交回契约"，否则模型仍会先去 load。"""
    for text in (
        load_prompt("node_execution.md"),
        load_prompt("acceptance_execution.md"),
        CeoPromptBuilder(loop=SimpleNamespace(workspace=None))._read_prompt("ceo_frontdoor.md"),
    ):
        assert "TOOL_CONTRACT_V1" in text
        assert "不执行" in text


def _receipt(container: dict, scope_key: str) -> str:
    return impulse_receipt(
        tool_name="filesystem_delete",
        contract_block="[TOOL_CONTRACT_V1:filesystem_delete:tcf:aaaa] 契约正文",
        outcome="staged",
        staged_count=3,
        targets_text=render_staged_targets(container, scope_key=scope_key),
    )["content"]


def test_impulse_receipt_enumerates_every_staged_target(tmp_path: Path) -> None:
    container: dict = {}
    for path in ("reports/a.txt", "reports/b.txt", "reports/c.txt"):
        container, _item, _count = tool_arg_stash.stage_args(
            container,
            scope_key="scope-1",
            args={"path": path, "recursive": False},
            fingerprint="tcf:aaaaaaaaaaaaaaaa",
            staged_hop=3,
        )

    content = _receipt(container, "scope-1")

    assert "sets: 3" in content
    for path in ("reports/a.txt", "reports/b.txt", "reports/c.txt"):
        assert path in content
    assert json.dumps({"path": "reports/a.txt", "recursive": False}, ensure_ascii=False) in content


def test_impulse_receipt_without_stash_omits_the_target_block() -> None:
    """容器空 ⇒ 不凭空造目标行：列出的必须真是已暂存的那几份。"""
    content = _receipt({}, "scope-1")

    assert "Staged sets that reuse_args=true would execute" not in content
