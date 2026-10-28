from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_model_config_role_routes_use_four_parallel_columns_without_header_copy() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    model_start = html.index('<section id="view-models"')
    model_end = html.index('<section id="view-task-details"', model_start)
    model_html = html[model_start:model_end]

    assert "<h3>Role Routes</h3>" not in model_html
    assert "继续使用拖拽排序编排 Leader / Execution / Inspection 的模型链。" not in model_html
    assert 'id="model-role-editors"' in model_html

    assert re.search(
        r"\.model-routing-grid\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\);",
        css,
        flags=re.MULTILINE,
    )


def test_role_chain_scroll_survives_editor_open_rerender() -> None:
    llm_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_llm.js").read_text(encoding="utf-8")
    start = llm_js.index("function renderRoutes()")
    body = llm_js[start:llm_js.index("function renderAll()", start)]

    # 打开/关闭某个模型配置会走 renderAll：角色链容器整块重建，每个 .model-chain-list
    # 都是新节点。重建前按角色记下滚动位置、重建后还回去，否则链的滚动条会回到最上面。
    assert body.count('querySelectorAll("[data-model-chain-list]")') == 2
    capture = body.index("chainScrollTops.set")
    rewrite = body.index("U.modelRoleEditors.innerHTML")
    restore = body.index("list.scrollTop = top")
    assert capture < rewrite < restore
