from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_llm_binding_editor_uses_styled_image_multimodal_checkbox_copy_and_layout() -> None:
    llm_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_llm.js").read_text(encoding="utf-8")
    llm_css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert "是否为图像多模态" in llm_js
    assert "llm-image-checkbox-field" in llm_js
    assert "llm-image-checkbox-control" in llm_js
    assert "llm-image-checkbox-indicator" in llm_js
    assert "Image Multimodal" not in llm_js
    assert "communication-toggle llm-image-toggle-control" not in llm_js
    assert ".llm-image-checkbox-control" in llm_css
    assert ".llm-image-checkbox-indicator" in llm_css


def test_memory_role_fixed_concurrency_reuses_segmented_label_shell_styling() -> None:
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")
    app_css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")

    assert "model-role-limit-fixed-track" in app_js
    assert "llm-segmented-label model-role-limit-fixed-pill" in app_js
    assert "grid-column: 2 / 3;" in app_css
    assert ".model-role-limit-fixed-pill {" in app_css
