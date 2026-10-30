"""头部沙箱声明：切档立即重印，受限档声明覆盖工作目录/CLI/工具面边界。"""

from __future__ import annotations

import main  # noqa: F401  (production import order)
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder


class _Loop:
    workspace = None


def _builder() -> CeoPromptBuilder:
    return CeoPromptBuilder(loop=_Loop())


def _render(*, stamp: str, tier: str) -> str:
    return _builder().build_base_prompt(cache_stamp=stamp, sandbox_tier=tier)


def test_jailed_session_declares_the_jail() -> None:
    text = _render(stamp="s1", tier="jailed")
    assert "## Sandbox Environment" in text
    assert "## Read-only Session" not in text
    # 三件模型最会猜错的事：卷、宿主路径、网络。
    assert "/workspace" in text and "/shared" in text
    assert "network=none" in text


def test_readonly_session_declares_the_read_boundary() -> None:
    text = _render(stamp="s1", tier="readonly")
    assert "## Read-only Session" in text
    assert "## Sandbox Environment" not in text


def test_limited_session_declares_the_workspace_and_cli_boundary() -> None:
    text = _render(stamp="s1", tier="limited")
    assert "## Limited Session" in text
    assert "本会话工作目录" in text
    assert "默认不提供命令行执行" in text
    assert "web_fetch" in text


def test_host_full_session_carries_no_declaration() -> None:
    text = _render(stamp="s1", tier="host_full")
    assert "## Sandbox Environment" not in text
    assert "## Read-only Session" not in text


def test_tier_change_reprints_immediately() -> None:
    """切档立即换头部：旧声明不能留在下一回合。"""
    builder = _builder()
    first = builder.build_base_prompt(cache_stamp="stamp-a", sandbox_tier="jailed")
    same_stamp = builder.build_base_prompt(cache_stamp="stamp-a", sandbox_tier="limited")
    assert "## Limited Session" in same_stamp
    assert "## Sandbox Environment" not in same_stamp

    next_reprint = builder.build_base_prompt(cache_stamp="stamp-b", sandbox_tier="limited")
    assert "## Limited Session" in next_reprint
    assert first != next_reprint
