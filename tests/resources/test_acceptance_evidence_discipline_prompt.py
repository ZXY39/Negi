"""验收节点提示词的证据纪律契约。

背景（2026-09-17 task:77d0ae460cf0）：验收节点把 content 工具对二进制文件返回的占位串统计
当成"实测文件大小"，把 430 个有效 PDF 判成 33-44 字节空壳，并升级为"伪造审计证据"指控。
这些条款是那类误判的护栏：没测过的数字不能写成实测、占位串统计不能当文件事实、
样本名必须取自权威清单、指控需要可引用的原始证据。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "main/prompts/acceptance_execution.md"


def _prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def test_acceptance_prompt_has_evidence_discipline_section() -> None:
    assert "### 3.4 证据纪律（测量、抽样与指控）" in _prompt()


def test_acceptance_prompt_forbids_unmeasured_numbers() -> None:
    prompt = _prompt()
    assert "工具从未返回过的数字" in prompt
    assert "不得以“实测”名义出现" in prompt
    # 测量能力缺失时的正确出口是 blocked，而不是用推断值补齐。
    assert 'failed + delivery_status="blocked"' in prompt


def test_acceptance_prompt_pins_binary_evidence_sources() -> None:
    prompt = _prompt()
    assert "`size_bytes`（磁盘真实字节数）" in prompt
    assert "`binary` / `content_display_replaced`" in prompt
    assert "`byte_level: true`" in prompt
    assert "只描述占位串本身" in prompt


def test_acceptance_prompt_names_read_only_measurement_tool() -> None:
    prompt = _prompt()
    # 命名具体工具，验收节点才知道除了内容通道还有一条能实测的只读通道。
    assert "`filesystem_stat`" in prompt
    assert "`file_count` / `total_bytes`" in prompt


def test_acceptance_prompt_requires_authoritative_sample_names() -> None:
    prompt = _prompt()
    assert "必须取自权威清单" in prompt
    assert "不得凭“类别 + 序号”自行拼出文件名" in prompt
    assert "`path not found` 只证明你给出的路径不存在" in prompt


def test_acceptance_prompt_rejects_invariant_observation_as_proof() -> None:
    prompt = _prompt()
    assert "不是证据" in prompt
    assert "输出逐字相同" in prompt


def test_acceptance_prompt_raises_bar_for_fabrication_accusations() -> None:
    prompt = _prompt()
    assert "“伪造 / 造假 / 虚构证据”一类指控有更高门槛" in prompt
    assert "至少用一条独立通道复核" in prompt
    assert "不得以指控代替结论" in prompt
