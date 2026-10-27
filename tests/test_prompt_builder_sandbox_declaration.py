"""头部沙箱声明：档位变化只在重印边界换字，不跟着切档立刻重印。"""

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


def test_host_full_session_carries_no_declaration() -> None:
    text = _render(stamp="s1", tier="host_full")
    assert "## Sandbox Environment" not in text
    assert "## Read-only Session" not in text


def test_tier_change_waits_for_the_next_reprint() -> None:
    """切档不重印前缀：同一 stamp 下换档，头部保持上一版声明。"""
    builder = _builder()
    first = builder.build_base_prompt(cache_stamp="stamp-a", sandbox_tier="jailed")
    same_stamp = builder.build_base_prompt(cache_stamp="stamp-a", sandbox_tier="readonly")
    assert "## Sandbox Environment" in same_stamp
    assert "## Read-only Session" not in same_stamp

    # 重印时钟（记忆采纳 / 工具面修订）动了，新档位才进头部。
    next_reprint = builder.build_base_prompt(cache_stamp="stamp-b", sandbox_tier="readonly")
    assert "## Read-only Session" in next_reprint
    assert "## Sandbox Environment" not in next_reprint
    assert first != next_reprint
