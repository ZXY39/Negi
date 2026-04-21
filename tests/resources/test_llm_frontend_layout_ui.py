from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_llm_binding_editor_uses_image_multimodal_toggle_copy_and_layout() -> None:
    llm_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_llm.js").read_text(encoding="utf-8")

    assert "是否为图像多模态" in llm_js
    assert "llm-image-toggle-field" in llm_js
    assert "communication-toggle llm-image-toggle-control" in llm_js
    assert "Image Multimodal" not in llm_js
    assert "llm-checkbox-field" not in llm_js


def test_memory_role_fixed_concurrency_uses_single_segment_width_instead_of_spanning_two_columns() -> None:
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")
    app_css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert "model-role-limit-fixed-track" in app_js
    assert "grid-column: 2 / 3;" in app_css
    assert "grid-column: 2 / 4;" not in app_css
