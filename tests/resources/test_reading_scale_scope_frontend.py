"""字号档位的作用范围：会话阅读面的文字都乘 --reading-font-scale，控件不跟。"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 会话阅读面：气泡与 markdown、中途输出、阶段/轮次标题、工具卡标签与详情、
# 节点详情的消息正文。每条都以中间输出或标题的身份出现在阅读面上。
READING_RULES = (
    ".message .msg-content.msg-content",
    ".markdown-content h1",
    ".markdown-content h2",
    ".interaction-step-title",
    ".interaction-step-status",
    ".interaction-step-detail",
    ".interaction-step-preview",
    ".task-trace-code",
    ".task-trace-round-thinking",
    ".task-trace-round-chip-title",
    ".task-trace-label",
    ".task-trace-message-body",
    ".spawn-node-text",
    ".interaction-flow-usage",
    ".task-flow-header",
    ".task-flow-usage",
    ".ceo-turn-usage",
    ".msg-meta",
    ".ceo-silent-line",
    ".ceo-thinking-line",
)

# 控件与页面框架：字号档位不许碰它们，否则按钮/侧栏会跟正文脱节。
FIXED_RULES = (
    ".msg-action-btn",
    ".interaction-flow-toggle",
    ":root .nav-item",
    ".ceo-session-title",
)

# 文本滚动框：封顶乘同一个倍数，视窗保持「约多少行」。
SCALED_CAPS = (
    ".task-trace-code",
    ".task-trace-message-body",
    ".spawn-node-text",
    ".ceo-thinking-line-pane",
)


def _source(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _rule_bodies(css: str) -> dict[str, list[str]]:
    """选择器 → 声明体列表（文件顺序）。同一条规则可能被后续规则追加声明。"""
    blocks: dict[str, list[str]] = {}
    # 注释不参与选择器匹配：否则紧贴注释的规则会被整段吞进上一个匹配里。
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    for match in re.finditer(r"(?ms)^([^@{}\n][^{}]*)\{([^{}]*)\}", css):
        selector = " ".join(match.group(1).split())
        blocks.setdefault(selector, []).append(match.group(2))
    return blocks


def _declaration(blocks: dict[str, list[str]], selector: str, prop: str) -> str | None:
    # 字号与封顶可能写在合并选择器里，后续同名规则只追加别的属性；
    # 从文件末尾往前找，第一条声明了该属性的规则就是层叠胜者。
    for selector_list, bodies in blocks.items():
        parts = [" ".join(part.split()) for part in selector_list.split(",")]
        if selector not in parts:
            continue
        for body in reversed(bodies):
            found = re.search(rf"(?<![\w-]){re.escape(prop)}:\s*([^;]+);", body)
            if found:
                return found.group(1).strip()
    return None


def test_reading_surfaces_multiply_the_scale() -> None:
    blocks = _rule_bodies(_source("g3ku/web/frontend/org_graph.css"))

    for selector in READING_RULES:
        font_size = _declaration(blocks, selector, "font-size")
        assert font_size is not None, f"{selector} 没有字号声明"
        assert "var(--reading-font-scale)" in font_size, f"{selector} 的字号没跟字号档位：{font_size}"


def test_controls_keep_their_own_type_size() -> None:
    blocks = _rule_bodies(_source("g3ku/web/frontend/org_graph.css"))

    for selector in FIXED_RULES:
        font_size = _declaration(blocks, selector, "font-size")
        assert font_size is not None, f"{selector} 没有字号声明"
        assert "var(--reading-font-scale)" not in font_size, f"{selector} 是控件，不该跟阅读字号"


def test_text_scroll_caps_follow_the_scale() -> None:
    blocks = _rule_bodies(_source("g3ku/web/frontend/org_graph.css"))

    for selector in SCALED_CAPS:
        max_height = _declaration(blocks, selector, "max-height")
        assert max_height is not None, f"{selector} 没有 max-height"
        assert "var(--reading-font-scale)" in max_height, f"{selector} 的封顶没跟字号：{max_height}"


def test_scale_commit_resyncs_flow_geometry() -> None:
    app_js = _source("g3ku/web/frontend/org_graph_app.js")

    assert "function syncReadingTextGeometry()" in app_js
    assert "syncCeoFlowGuideLength({ flowEl: flow, listEl: list })" in app_js
    assert "onCommit: syncReadingTextGeometry" in app_js
