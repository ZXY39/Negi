from __future__ import annotations

import asyncio
from pathlib import Path

import g3ku.web.main as web_main


def _serve(tmp_path: Path, monkeypatch, name: str):
    (tmp_path / "org_graph.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "app.js").write_text("window.app = 1;", encoding="utf-8")
    (tmp_path / "out.txt").write_text("debug dump", encoding="utf-8")
    (tmp_path / "search.py").write_text("print(1)", encoding="utf-8")
    monkeypatch.setattr(web_main, "FRONTEND_DIR", tmp_path)
    monkeypatch.setattr(web_main, "FRONTEND_ENTRY", tmp_path / "org_graph.html")
    return asyncio.run(web_main.serve_frontend_file(name))


def test_stray_text_file_falls_back_to_spa(tmp_path, monkeypatch):
    response = _serve(tmp_path, monkeypatch, "out.txt")
    assert Path(response.path) == tmp_path / "org_graph.html"


def test_stray_script_file_falls_back_to_spa(tmp_path, monkeypatch):
    response = _serve(tmp_path, monkeypatch, "search.py")
    assert Path(response.path) == tmp_path / "org_graph.html"


def test_declared_asset_suffix_is_served(tmp_path, monkeypatch):
    response = _serve(tmp_path, monkeypatch, "app.js")
    assert Path(response.path) == tmp_path / "app.js"


def test_missing_path_still_falls_back_to_spa(tmp_path, monkeypatch):
    response = _serve(tmp_path, monkeypatch, "does-not-exist.js")
    assert Path(response.path) == tmp_path / "org_graph.html"
