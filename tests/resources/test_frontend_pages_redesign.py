from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "g3ku/web/frontend"


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_model_page_has_a_single_view_layer() -> None:
    html = _read("org_graph.html")
    llm_js = _read("org_graph_llm.js")

    # 两代渲染层已合并：旧视图文件不存在，脚本清单里只有新的两个分片。
    assert not (FRONTEND / "models/role-view.js").exists()
    assert "models/group-view.js" in html
    assert "models/dialog-view.js" in html
    # 覆盖式赋值与其内部旧名都已消失，四个入口只定义一次。
    assert "renderBindings" not in llm_js
    assert "window.renderModelList = renderModelList;" in llm_js
    assert "window.renderModelRoleEditors = renderModelRoleEditors;" in llm_js
    assert "window.renderModelDetail = renderModelDetail;" in llm_js
    assert "window.renderModelCatalog = renderModelCatalog;" in llm_js


def test_model_page_max_iterations_is_per_role() -> None:
    html = _read("org_graph.html")
    llm_js = _read("org_graph_llm.js")

    # 每角色卡头各带一个输入，钩子与 syncRoleIterationDraftsFromInputs 共用。
    assert 'data-model-role-limit-kind="iterations"' in llm_js
    assert 'data-model-role-limit-scope="${escv(scope.key)}"' in llm_js
    assert 'data-model-role-limit-input="iterations"' in llm_js
    assert 'modelScopeIterations(scope.key, iterationSource)' in llm_js
    # 顶部那颗一次写三个 scope 的全局输入已删除（其处理器模式随之消失）。
    assert "model-iterations-toggle-btn" not in html
    assert "model-iterations-value-input" not in html
    assert "roleIterationDrafts[scope] = next" not in llm_js


def test_model_page_layout_is_library_plus_routes() -> None:
    html = _read("org_graph.html")
    css = _read("org_graph.css")

    assert 'class="llm-library-panel"' in html
    assert html.index('id="llm-bindings-list"') < html.index('id="model-group-list"')
    assert "grid-template-columns: minmax(280px, 340px) minmax(0, 1fr);" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css


def test_external_page_groups_tokens_and_tracks_unsaved_edits() -> None:
    js = _read("org_graph_external.js")

    # token 按来源分组：QQ 账号前缀归入账号组，其余归入桥接应用组。
    assert "function _externalTokenGroups" in js
    assert 'push("bridges", "桥接应用"' in js
    # 危险操作一律二次确认：重新生成与删除同级。
    assert "function regenerateExternalToken" in js
    assert 'title: "重新生成 token"' in js
    # 文本编辑走脏位提示，不再是「改了但看不出来」。
    assert "function _markQqBotDirty" in js
    assert "is-dirty" in js
    # 账号默认只读摘要，展开才编辑。
    assert 'createElement("details")' in js
    assert "qq-bot-account-row" in js
