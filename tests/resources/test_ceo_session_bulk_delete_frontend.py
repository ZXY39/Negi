from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ceo_session_bulk_delete_markup_and_branding() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")

    assert "<title>Negi</title>" in html
    assert '<img class="brand-icon" src="favicon.ico" alt="Negi">' in html
    assert '<span class="brand-text">Negi</span>' in html
    assert 'id="ceo-session-bulk-toggle"' in html
    assert 'id="ceo-session-bulk-actions"' in html
    assert 'id="ceo-session-bulk-delete"' in html
    assert 'id="ceo-session-bulk-select-all"' in html
    assert "G3ku Main Runtime" not in html


def test_ceo_session_bulk_delete_css_contract() -> None:
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    tabs_match = re.search(
        r"\.ceo-shell\.is-session-panel-expanded \.ceo-session-tabs\s*\{(?P<body>[^}]+)\}",
        css,
        flags=re.MULTILINE,
    )

    assert tabs_match is not None
    assert "--ceo-session-tab-gap: 8px;" in tabs_match.group("body")
    assert ".ceo-session-bulk-toggle" in css
    assert ".ceo-session-bulk-actions" in css
    assert ".ceo-session-checkbox" in css
    assert re.search(r"\.ceo-session-tab\s*\{[^}]*white-space:\s*nowrap;", css, flags=re.MULTILINE)
    assert re.search(r"\.confirm-dialog\s*\{[^}]*max-height:\s*min\(720px,\s*calc\(100vh - 48px\)\);", css, flags=re.MULTILINE)
    assert re.search(r"\.confirm-text\s*\{[^}]*overflow-y:\s*auto;", css, flags=re.MULTILINE)
    assert re.search(r"\.confirm-checkbox-details\s*\{[^}]*overflow:\s*auto;", css, flags=re.MULTILINE)
    assert ".resource-header-search" in css
    assert re.search(r"\.compact-resource-header-actions\s*\{[^}]*flex-wrap:\s*nowrap;", css, flags=re.MULTILINE)
    assert re.search(r"\.compact-resource-header-actions\s*\{[^}]*overflow:\s*visible;", css, flags=re.MULTILINE)
    assert re.search(r"\.resource-header-search\s*\{[^}]*border-radius:\s*14px;", css, flags=re.MULTILINE)
    assert re.search(r"\.compact-resource-header-actions\s*>\s*\.toolbar-btn\s*\{[^}]*white-space:\s*nowrap;", css, flags=re.MULTILINE)
    assert re.search(r"\.compact-resource-header-actions\s*>\s*\.toolbar-btn\s*\{[^}]*min-height:\s*42px;", css, flags=re.MULTILINE)
    assert re.search(r"\.compact-resource-header-actions\s*>\s*\.toolbar-btn\s*\{[^}]*height:\s*42px;", css, flags=re.MULTILINE)
    assert re.search(r"\.compact-resource-header-actions\s*>\s*\.toolbar-btn\s*\{[^}]*min-width:\s*0;", css, flags=re.MULTILINE)
    assert re.search(r"\.compact-resource-header-actions\s*>\s*\.resource-select-shell\s+\.resource-select-trigger\s*\{[^}]*min-height:\s*42px;", css, flags=re.MULTILINE)
    assert re.search(r"\.compact-resource-header-actions\s*>\s*\.resource-select-shell\s+\.resource-select-trigger\s*\{[^}]*height:\s*42px;", css, flags=re.MULTILINE)
    assert re.search(r"\.compact-resource-header-actions\s*>\s*\.resource-select-shell\s+\.resource-select-trigger\s*\{[^}]*border-radius:\s*14px;", css, flags=re.MULTILINE)
    assert re.search(r"\.compact-resource-header-actions\s*>\s*\.resource-header-search\s*\{[^}]*height:\s*42px;", css, flags=re.MULTILINE)
    assert re.search(r"\.resource-list\s*\{[^}]*padding:\s*var\(--space-4\)\s+var\(--space-4\)\s+var\(--space-4\);", css, flags=re.MULTILINE)
    generic_search_index = css.index(".resource-search,\n.resource-select {")
    compact_search_override_index = css.rfind(".compact-resource-header-actions > .resource-header-search {")
    compact_select_override_index = css.rfind(".compact-resource-header-actions > .resource-select-shell .resource-select-trigger {")
    assert compact_search_override_index > generic_search_index
    assert compact_select_override_index > generic_search_index


def test_ceo_session_checkbox_render_contract() -> None:
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    input_rule = re.search(r"\.ceo-session-checkbox input\s*\{(?P<body>[^}]+)\}", css)
    box_rule = re.search(r"\.ceo-session-checkbox__box\s*\{(?P<body>[^}]+)\}", css)
    check_rule = re.search(r"\.ceo-session-checkbox__box::after\s*\{(?P<body>[^}]+)\}", css)

    assert input_rule is not None
    assert box_rule is not None
    assert check_rule is not None
    # 原生 input 透明铺满 label 负责交互，视觉由 __box 自绘，不再依赖 accent-color
    assert "opacity: 0;" in input_rule.group("body")
    assert "accent-color" not in input_rule.group("body")
    # 对勾元素靠 flex 居中（不得用 left/top 手调定位），并补偿旋转后的视觉重心；
    # 否则勾号整体偏下，叠加卡片的小数 y 取值后各卡片取整方向不同，看起来高低不一。
    assert "align-items: center;" in box_rule.group("body")
    assert "justify-content: center;" in box_rule.group("body")
    assert "position: absolute" not in check_rule.group("body")
    assert "transform: translate(0, -2.12px) rotate(45deg);" in check_rule.group("body")
    # 收缩态窄栏只留图标，勾选框不得残留
    assert re.search(
        r'\.ceo-session-panel\[data-panel-state="collapsed"\] \.ceo-session-checkbox\s*\{[^}]*display: none !important;',
        css,
        flags=re.MULTILINE,
    )


def test_resource_headers_and_ceo_bulk_actions_follow_latest_layout() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")

    assert "通过 CEO 前门与 main 运行时交互" not in html
    assert "查看、编辑 Skill 文件内容与角色可见性策略" not in html
    assert "查看工具族、各 action 权限，以及当前资源可用状态" not in html
    assert "选择供应商并维护模型 JSON 配置，再将模型编排到 Role Routes" not in html
    assert "统一管理 QQ Bot、钉钉、企微与飞书通信" not in html

    ceo_section = re.search(r'<section id="view-ceo".*?</section>', html, flags=re.DOTALL)
    assert ceo_section is not None
    ceo_html = ceo_section.group(0)
    assert ceo_html.index('id="ceo-session-bulk-select-all"') < ceo_html.index('id="ceo-session-bulk-delete"')
    assert ceo_html.index('id="ceo-session-bulk-actions"') < ceo_html.index('id="ceo-session-list"')

    skill_section = re.search(r'<section id="view-skills".*?</section>', html, flags=re.DOTALL)
    assert skill_section is not None
    skill_html = skill_section.group(0)
    assert 'id="skill-save-btn"' not in skill_html
    assert 'id="tool-governance-banner"' not in skill_html
    assert skill_html.index('id="skill-risk-filter"') < skill_html.index('id="skill-status-filter"')
    assert skill_html.index('id="skill-status-filter"') < skill_html.index('id="skill-search-input"')
    # 两个筛选并列后 value=all 的占位项按筛选名标注，不再共用「全部」
    assert skill_html.count('<option value="all">风险</option>') == 1
    assert skill_html.count('<option value="all">状态</option>') == 1

    tool_section = re.search(r'<section id="view-tools".*?</section>', html, flags=re.DOTALL)
    assert tool_section is not None
    tool_html = tool_section.group(0)
    assert 'id="tool-save-btn"' not in tool_html
    assert 'id="tool-governance-banner"' in tool_html
    assert tool_html.index('id="tool-refresh-btn"') < tool_html.index('id="tool-governance-banner"')
    assert tool_html.index('id="tool-governance-banner"') < tool_html.index('id="tool-risk-filter"')
    assert tool_html.index('id="tool-risk-filter"') < tool_html.index('id="tool-status-filter"')
    assert tool_html.index('id="tool-status-filter"') < tool_html.index('id="tool-search-input"')
    assert tool_html.count('<option value="all">风险</option>') == 1
    assert tool_html.count('<option value="all">状态</option>') == 1

    model_section = re.search(r'<section id="view-models".*?</section>', html, flags=re.DOTALL)
    assert model_section is not None
    model_html = model_section.group(0)
    assert '<h1>模型配置</h1>' in model_html
    assert 'id="model-refresh-btn"' in model_html
    assert 'id="llm-config-create-btn" class="toolbar-btn ghost"' in model_html
    assert 'id="model-roles-save-btn"' in model_html

    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")
    resources_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_resources.js").read_text(encoding="utf-8")
    llm_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_llm.js").read_text(encoding="utf-8")
    assert "当前首选" not in app_js
    assert "当前首选" not in llm_js
    assert "skill-modal-save" in resources_js
    assert "tool-modal-save" in resources_js
    assert "queueSkillAutosave(1200)" not in resources_js
    assert "queueToolAutosave(120)" not in resources_js
    assert "将自动保存" not in app_js
    assert "resource-select-option-check" not in app_js
    assert '(isCoreTool && agentVisible && role === "ceo")' not in resources_js
    assert '当前 action 对所有角色禁用。' in resources_js

    # The China channel subsystem and its "通信配置" panel have been removed;
    # the web UI exposes external channel access via the External Agent API.
    assert '<section id="view-communications"' not in html
    assert 'data-view="communications"' not in html
