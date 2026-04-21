from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_frontend_model_and_session_wording_matches_latest_copy() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")
    llm_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_llm.js").read_text(encoding="utf-8")
    api_client_js = (REPO_ROOT / "g3ku/web/frontend/api_client.js").read_text(encoding="utf-8")

    assert "会话列表" in html
    assert "Leader 回合" in html
    assert 'aria-label="会话列表"' in html
    assert 'aria-label="会话视图"' in html
    assert 'aria-label="会话批量操作"' in html
    assert 'id="llm-memory-settings-btn" class="toolbar-btn ghost" type="button">RAG模型设置</button>' in html
    assert 'id="llm-memory-settings-btn" class="toolbar-btn ghost" type="button">记忆模型设置</button>' not in html
    assert "<span>Leader 会话</span>" not in html
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
    assert '<span class="llm-segmented-label model-role-limit-fixed-pill">固定为1</span>' in app_js
    assert '<span class="policy-chip neutral">固定 1</span>' not in app_js
    assert '<span class="policy-chip neutral">只读</span>' not in app_js

    assert 'const SCOPE_LABELS = { ceo: "主Agent", execution: "执行Agent", inspection: "检验Agent", memory: "记忆Agent" };' in llm_js
    assert 'const SCOPE_LABELS = { ceo: "CEO", execution: "Execution", inspection: "Inspection" };' not in llm_js
    assert llm_js.count("<h2>RAG模型设置</h2>") == 2
    assert "<h2>记忆模型设置</h2>" not in llm_js

    assert "主Agent（Leader）角色" in api_client_js
    assert "主Agent（CEO）角色" not in api_client_js
