from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ceo_chat_feed_uses_compact_left_padding_without_avatar_gap() -> None:
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert re.search(
        r"\.chat-feed\s*\{[^}]*padding:\s*clamp\(24px,\s*3vw,\s*36px\)\s+clamp\(28px,\s*6vw,\s*80px\)\s+calc\(var\(--space-6\)\s*\+\s*156px\)\s+clamp\(16px,\s*2\.4vw,\s*32px\);",
        css,
        flags=re.MULTILINE,
    )


def test_ceo_approval_viewport_is_centered_above_chat_input() -> None:
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert re.search(
        r"\.ceo-approval-viewport\s*\{[^}]*position:\s*absolute;[^}]*left:\s*0;[^}]*right:\s*0;[^}]*bottom:\s*calc\(100%\s*\+\s*12px\);[^}]*justify-content:\s*center;",
        css,
        flags=re.MULTILINE,
    )


def test_app_sidebars_use_compact_160px_width_before_mobile_stack() -> None:
    css_files = [
        REPO_ROOT / "g3ku/web/frontend/org_graph.css",
        REPO_ROOT / "g3ku/web/frontend/search.css",
    ]

    for css_path in css_files:
        css = css_path.read_text(encoding="utf-8")

        assert re.search(
            r"\.sidebar\s*\{[^}]*width:\s*160px;",
            css,
            flags=re.MULTILINE,
        ), css_path.as_posix()
        assert re.search(
            r"@media\s*\(max-width:\s*768px\)\s*\{[^{}]*\.sidebar\s*\{[^}]*width:\s*160px;",
            css,
            flags=re.MULTILINE | re.DOTALL,
        ), css_path.as_posix()
