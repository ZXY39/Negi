from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ceo_session_bulk_delete_markup_and_branding() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")

    assert "<title>G3KU</title>" in html
    assert '<span class="brand-text">G3KU</span>' in html
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
    assert re.search(r"\.communication-compact-header\s*\{[^}]*padding-top:\s*var\(--space-4\);", css, flags=re.MULTILINE)
    assert re.search(r"\.communication-compact-header\s*\{[^}]*padding-bottom:\s*var\(--space-4\);", css, flags=re.MULTILINE)
    assert not re.search(r"\.communication-compact-header\s*\{[^}]*height:\s*42px;", css, flags=re.MULTILINE)


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
    assert skill_html.index('id="skill-risk-filter"') < skill_html.index('id="skill-status-filter"')
    assert skill_html.index('id="skill-status-filter"') < skill_html.index('id="skill-search-input"')
    assert skill_html.count('>全部</option>') == 2

    tool_section = re.search(r'<section id="view-tools".*?</section>', html, flags=re.DOTALL)
    assert tool_section is not None
    tool_html = tool_section.group(0)
    assert 'id="tool-save-btn"' not in tool_html
    assert tool_html.index('id="tool-risk-filter"') < tool_html.index('id="tool-status-filter"')
    assert tool_html.index('id="tool-status-filter"') < tool_html.index('id="tool-search-input"')
    assert tool_html.count('>全部</option>') == 2

    model_section = re.search(r'<section id="view-models".*?</section>', html, flags=re.DOTALL)
    assert model_section is not None
    model_html = model_section.group(0)
    assert '<h1>模型配置</h1>' in model_html
    assert 'id="model-refresh-btn"' in model_html
    assert 'id="llm-memory-settings-btn"' in model_html
    assert 'id="llm-config-create-btn" class="toolbar-btn ghost"' in model_html
    assert 'id="model-roles-save-btn"' in model_html

    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")
    resources_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_resources.js").read_text(encoding="utf-8")
    llm_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_llm.js").read_text(encoding="utf-8")
    assert "当前首选" not in app_js
    assert "当前首选" not in llm_js
    assert "skill-modal-save" not in resources_js
    assert "tool-modal-save" not in resources_js
    assert "resource-select-option-check" not in app_js
    assert '(isCoreTool && agentVisible && role === "ceo")' not in resources_js
    assert '当前 action 对所有角色禁用。' in resources_js

    communication_section = re.search(r'<section id="view-communications".*?</section>', html, flags=re.DOTALL)
    assert communication_section is not None
    communication_html = communication_section.group(0)
    assert 'communication-compact-header' in communication_html
    assert communication_html.index('<h1>通信配置</h1>') < communication_html.index('id="communication-refresh-btn"')
