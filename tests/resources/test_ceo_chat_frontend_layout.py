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
