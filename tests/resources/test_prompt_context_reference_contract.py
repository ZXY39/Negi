"""提示词的「指称对齐」与「单一权威源」契约。

背景（2026-09-21 提示词理解性审计）：13 个提示词文件只是模型上下文的一部分，预算计数、
免预算名单、失败分类这些内容由运行时 overlay / 工具 schema / 事件束注入。两类缺陷反复出现：
- 提示词引用的名字与模型实际看到的注入块抬头对不上，模型无法把规则落到证据上；
- 同一条契约在提示词与运行时文本里各写一遍并逐渐分叉（阻塞核验曾有两份分支表）。
本组测试锁定修复后的口径：引用真实抬头名，副本只留指针。
"""

from __future__ import annotations

from pathlib import Path

from main.prompts import load_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]
# 节点道的合同条款现在住在 shared_*.md 里，由 main/prompts.py 展开。断言必须读**渲染结果**：
# 读原始文件会漏看 partial 里的条款，那等于测试一个模型根本看不到的形状。
NODE_PROMPT = load_prompt("node_execution.md")
ACCEPTANCE_PROMPT = load_prompt("acceptance_execution.md")
CEO_PROMPT = (REPO_ROOT / "g3ku/runtime/prompts/ceo_frontdoor.md").read_text(encoding="utf-8")

OVERLAY_HEADER = "System note for this turn only:"






def test_frontdoor_failure_rules_bind_to_rendered_fields() -> None:
    assert "Failure class:" in CEO_PROMPT
    assert "non_retryable_blocked" in CEO_PROMPT
    # 任务级重试禁令必须与 heartbeat 的节点级 resume 划清边界。
    assert "节点级 `resume`" in CEO_PROMPT




def test_node_prompt_states_ledger_and_wording_check_semantics() -> None:
    # 执行节点的提交没有 status/delivery_status：提交即完成；措辞自检不是字符串匹配。
    assert "提交即表示" in NODE_PROMPT
    assert "不是字符串匹配" in NODE_PROMPT
