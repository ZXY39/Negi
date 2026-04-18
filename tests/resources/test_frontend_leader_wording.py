from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_frontend_visible_ceo_wording_is_replaced_with_leader() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")
    llm_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_llm.js").read_text(encoding="utf-8")
    api_client_js = (REPO_ROOT / "g3ku/web/frontend/api_client.js").read_text(encoding="utf-8")

    assert "Leader 会话" in html
    assert "Leader 回合" in html
    assert 'aria-label="Leader session list"' in html
    assert 'aria-label="Leader session views"' in html
    assert 'aria-label="Leader session bulk actions"' in html
    assert "CEO 会话" not in html
    assert "CEO 回合" not in html
    assert "CEO session list" not in html
    assert "CEO session views" not in html
    assert "CEO session bulk actions" not in html

    assert "暂停当前 Leader 会话" in app_js
    assert "当前没有可暂停的 Leader 回合。" in app_js
    assert "不能在 Leader 面板直接发送。" in app_js
    assert "暂停当前 CEO 会话" not in app_js
    assert "当前没有可暂停的 CEO 回合。" not in app_js
    assert "不能在 CEO 面板直接发送。" not in app_js

    assert 'const SCOPE_LABELS = { ceo: "Leader", execution: "Execution", inspection: "Inspection", memory: "Memory" };' in llm_js
    assert 'const SCOPE_LABELS = { ceo: "CEO", execution: "Execution", inspection: "Inspection" };' not in llm_js

    assert "主Agent（Leader）角色" in api_client_js
    assert "主Agent（CEO）角色" not in api_client_js
