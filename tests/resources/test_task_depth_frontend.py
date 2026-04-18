from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_task_depth_presets_include_root_only_option() -> None:
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")

    assert "const TASK_DEPTH_PRESET_VALUES = Object.freeze([0, 1, 2, 3, 4, 5]);" in app_js
