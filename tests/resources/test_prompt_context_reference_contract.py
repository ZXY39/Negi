"""提示词的「指称对齐」与「单一权威源」契约。

背景（2026-09-21 提示词理解性审计）：13 个提示词文件只是模型上下文的一部分，预算计数、
免预算名单、失败分类这些内容由运行时 overlay / 工具 schema / 事件束注入。两类缺陷反复出现：
- 提示词引用的名字与模型实际看到的注入块抬头对不上，模型无法把规则落到证据上；
- 同一条契约在提示词与运行时文本里各写一遍并逐渐分叉（阻塞核验曾有两份分支表）。
本组测试锁定修复后的口径：引用真实抬头名，副本只留指针。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_PROMPT = (REPO_ROOT / "main/prompts/node_execution.md").read_text(encoding="utf-8")
ACCEPTANCE_PROMPT = (REPO_ROOT / "main/prompts/acceptance_execution.md").read_text(encoding="utf-8")
CEO_PROMPT = (REPO_ROOT / "g3ku/runtime/prompts/ceo_frontdoor.md").read_text(encoding="utf-8")
BLOCKED_PROMPT = (REPO_ROOT / "main/prompts/blocked_verification.md").read_text(encoding="utf-8")

OVERLAY_HEADER = "System note for this turn only:"


def test_stage_budget_rules_point_at_the_injected_overlay_header() -> None:
    for prompt in (NODE_PROMPT, ACCEPTANCE_PROMPT):
        assert OVERLAY_HEADER in prompt
        assert "stage_summary:" in prompt
    # 节点上下文里没有 rounds[*] 这个 JSON 路径，不能再把它当权威字段名引用。
    assert "rounds[*].budget_counted" not in NODE_PROMPT
    assert "rounds[*].budget_counted" not in ACCEPTANCE_PROMPT


def test_acceptance_free_tool_list_declares_overlay_as_authority() -> None:
    assert "该名单每轮由系统 overlay 重新列出" in ACCEPTANCE_PROMPT


def test_blocked_verification_defers_judgment_contract_to_acceptance_prompt() -> None:
    assert "权威定义在本节点系统提示词" in BLOCKED_PROMPT
    # 判定契约的副本只允许一句指针式摘要，分支表只能有一份。
    assert len(BLOCKED_PROMPT.split("## 判定契约", 1)[1]) < 700
    assert "阻塞成立 → `success`" in BLOCKED_PROMPT


def test_acceptance_prompt_keeps_one_terminal_blocked_branch() -> None:
    assert "终局失败，或核验本身无法完成" in ACCEPTANCE_PROMPT
    assert '**核验无法完成** → `failed`' not in ACCEPTANCE_PROMPT
    # §3.4 的缺测量能力出口必须与 §3.1 的 blocked 禁令互相指认。
    assert "不得用它规避必要检查" in ACCEPTANCE_PROMPT


def test_frontdoor_failure_rules_bind_to_rendered_fields() -> None:
    assert "Failure class:" in CEO_PROMPT
    assert "non_retryable_blocked" in CEO_PROMPT
    # 任务级重试禁令必须与 heartbeat 的节点级 resume 划清边界。
    assert "节点级 `resume`" in CEO_PROMPT


def test_frontdoor_stage_protocol_reference_points_the_right_way() -> None:
    assert "上面 §1.1「下一步协议」" in CEO_PROMPT
    assert "下面的“阶段优先协议”" not in CEO_PROMPT


def test_node_prompt_states_ledger_and_wording_check_semantics() -> None:
    assert "不自动加重处罚" in NODE_PROMPT
    assert "不是字符串匹配" in NODE_PROMPT
