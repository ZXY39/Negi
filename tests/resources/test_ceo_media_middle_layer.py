"""Tests for the CEO media middle layer (thumbnail staging + signed original viewer)."""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import ceo_media


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(ceo_media.router, prefix="/api")
    return TestClient(app)


def _write_noisy_png(path: Path, *, size=(1600, 1200), seed=7) -> None:
    from PIL import Image

    rnd = random.Random(seed)
    width, height = size
    img = Image.new("RGB", (width, height))
    img.putdata(
        [(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(width * height)]
    )
    img.save(path)


def _viewer_token(url: str) -> str:
    return url.split("token=", 1)[1]


def test_rewrite_stages_thumbnail_and_viewer_url(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "media" / "big.png"
    source.parent.mkdir()
    _write_noisy_png(source)
    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")

    content = (
        f"![big pic]({source})\n\n"
        f"[report]({note})\n\n"
        "[ext](https://example.com/x.png)\n\n"
        f"![missing]({tmp_path / 'nope.png'})"
    )
    rewritten = ceo_media.rewrite_assistant_media_content("sess_a", content)

    image_match = re.search(r'!\[big pic\]\(([^")]+) "([^"]+)"\)', rewritten)
    assert image_match, rewritten
    thumb = Path(image_match.group(1))
    viewer = image_match.group(2)
    assert thumb.exists()
    assert "inline_media" in str(thumb)
    assert thumb.stat().st_size <= ceo_media.THUMBNAIL_MAX_BYTES
    assert viewer.startswith(ceo_media.VIEWER_ROUTE + "?token=")

    link_match = re.search(r"\[report\]\(([^)]+)\)", rewritten)
    assert link_match and link_match.group(1).startswith(ceo_media.VIEWER_ROUTE + "?token=")
    assert "[ext](https://example.com/x.png)" in rewritten
    assert f"![missing]({tmp_path / 'nope.png'})" in rewritten


def test_thumbnail_idempotent_and_invalidated_on_change(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "photo.png"
    _write_noisy_png(source, size=(800, 600), seed=3)
    content = f"![p]({source})"

    first = ceo_media.rewrite_assistant_media_content("sess_b", content)
    second = ceo_media.rewrite_assistant_media_content("sess_b", content)
    first_thumb = re.search(r'!\[p\]\(([^")]+)', first).group(1)
    assert first_thumb in second

    _write_noisy_png(source, size=(800, 600), seed=4)
    third = ceo_media.rewrite_assistant_media_content("sess_b", content)
    third_thumb = re.search(r'!\[p\]\(([^")]+)', third).group(1)
    assert third_thumb != first_thumb


def test_original_endpoint_inline_attachment_and_tamper(monkeypatch, tmp_path, client):
    monkeypatch.chdir(tmp_path)
    image = tmp_path / "pic.png"
    _write_noisy_png(image, size=(64, 64), seed=1)
    svg = tmp_path / "evil.svg"
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")

    good = client.get(
        "/api/ceo/media/original",
        params={"token": _viewer_token(ceo_media.original_view_url(image))},
    )
    assert good.status_code == 200
    assert "inline" in good.headers.get("content-disposition", "")
    assert good.content == image.read_bytes()

    risky = client.get(
        "/api/ceo/media/original",
        params={"token": _viewer_token(ceo_media.original_view_url(svg))},
    )
    assert risky.status_code == 200
    assert "attachment" in risky.headers.get("content-disposition", "")

    token = _viewer_token(ceo_media.original_view_url(image))
    tampered = token[:-4] + ("0000" if not token.endswith("0000") else "1111")
    assert client.get("/api/ceo/media/original", params={"token": tampered}).status_code == 403


def test_original_endpoint_404_after_delete(monkeypatch, tmp_path, client):
    monkeypatch.chdir(tmp_path)
    image = tmp_path / "gone.png"
    _write_noisy_png(image, size=(64, 64), seed=2)
    token = _viewer_token(ceo_media.original_view_url(image))
    image.unlink()
    assert client.get("/api/ceo/media/original", params={"token": token}).status_code == 404
