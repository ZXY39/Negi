from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_model_config_role_routes_use_four_parallel_columns_without_header_copy() -> None:
    html = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    model_start = html.index('<section id="view-models"')
    model_end = html.index('<section id="view-communications"', model_start)
    model_html = html[model_start:model_end]

    assert "<h3>Role Routes</h3>" not in model_html
    assert "继续使用拖拽排序编排 Leader / Execution / Inspection 的模型链。" not in model_html
    assert 'id="model-role-editors"' in model_html

    assert re.search(
        r"\.model-routing-grid\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\);",
        css,
        flags=re.MULTILINE,
    )
