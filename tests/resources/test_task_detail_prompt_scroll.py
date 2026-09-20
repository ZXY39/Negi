"""任务详情页头部初始提示词的展开态契约：固定视口 + 滚轮滚动，不随提示词长度变化。"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(?P<body>.*?)\n\}", css, re.S)
    assert match is not None, f"missing rule: {selector}"
    return match.group("body")


def test_expanded_prompt_text_is_a_fixed_height_scroll_viewport() -> None:
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")
    body = _rule(css, ".task-prompt-disclosure[open] .task-prompt-text")

    assert "height: 120px;" in body
    assert "max-height" not in body
    assert "overflow-y: auto;" in body
    assert "overscroll-behavior: contain;" in body
    # 展开后必须换行，否则固定高度只会把超出部分裁掉。
    assert "white-space: normal;" in body
    # 折叠态仍是单行省略号，不受固定高度影响。
    collapsed = _rule(css, ".task-prompt-text")
    assert "white-space: nowrap;" in collapsed
    assert "text-overflow: ellipsis;" in collapsed


def test_prompt_disclosure_markup_stays_a_single_details_summary() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")

    assert 'id="td-prompt-disclosure"' in html
    assert 'id="td-prompt-text"' in html
    assert html.index('id="td-prompt-disclosure"') < html.index('id="td-prompt-text"')
