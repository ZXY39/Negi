from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime

from g3ku.web import frontend_assets


def _configure_font_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(frontend_assets, "FONT_VENDOR_DIR", tmp_path)
    monkeypatch.setattr(frontend_assets, "FONT_STYLESHEET_PATH", tmp_path / "google-fonts.css")
    monkeypatch.setattr(frontend_assets, "FONT_MANIFEST_PATH", tmp_path / "google-fonts-manifest.json")


def _configure_lucide_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(frontend_assets, "VENDOR_DIR", tmp_path)
    monkeypatch.setattr(frontend_assets, "LUCIDE_BUNDLE_PATH", tmp_path / "lucide.min.js")
    monkeypatch.setattr(frontend_assets, "LUCIDE_MANIFEST_PATH", tmp_path / "lucide-manifest.json")


def _make_lucide_tarball(bundle_text: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = bundle_text.encode("utf-8")
        info = tarfile.TarInfo("package/dist/umd/lucide.min.js")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_ensure_frontend_font_assets_rewrites_stylesheet_and_manifest(tmp_path, monkeypatch) -> None:
    _configure_font_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(
        frontend_assets,
        "GOOGLE_FONTS_CSS_URL",
        "https://fonts.googleapis.com/css2?family=Fixture",
    )
    monkeypatch.delenv("G3KU_FRONTEND_ASSET_UPDATE_MODE", raising=False)
    monkeypatch.delenv("G3KU_FRONTEND_FONT_UPDATE_MODE", raising=False)
    monkeypatch.delenv("G3KU_FRONTEND_FONT_REFRESH_FORCE", raising=False)
    monkeypatch.delenv("G3KU_FRONTEND_FONT_REFRESH_DAYS", raising=False)

    remote_a = "https://fonts.gstatic.com/s/firasans/v18/file-a.woff2"
    remote_b = "https://fonts.gstatic.com/s/firasans/v18/file-b.woff2"
    stylesheet = (
        "@font-face { font-family: 'Fira Sans'; src: url("
        f"{remote_a}"
        ") format('woff2'); }\n"
        "@font-face { font-family: 'Fira Sans'; src: url("
        f"{remote_b}"
        ") format('woff2'); }\n"
    )

    monkeypatch.setattr(frontend_assets, "_fetch_text", lambda url, *, headers: stylesheet)
    monkeypatch.setattr(
        frontend_assets,
        "_fetch_bytes",
        lambda url, *, headers: f"payload:{url.rsplit('/', 1)[-1]}".encode("utf-8"),
    )

    updated = frontend_assets.ensure_frontend_font_assets(force_refresh=True)

    assert updated is True
    assert frontend_assets.frontend_font_assets_available() is True
    saved_stylesheet = (tmp_path / "google-fonts.css").read_text(encoding="utf-8")
    assert remote_a not in saved_stylesheet
    assert remote_b not in saved_stylesheet
    assert "url('./file-a.woff2')" in saved_stylesheet
    assert "url('./file-b.woff2')" in saved_stylesheet
    assert (tmp_path / "file-a.woff2").read_bytes() == b"payload:file-a.woff2"
    assert (tmp_path / "file-b.woff2").read_bytes() == b"payload:file-b.woff2"

    manifest = json.loads((tmp_path / "google-fonts-manifest.json").read_text(encoding="utf-8"))
    assert manifest["stylesheet"] == "google-fonts.css"
    assert manifest["font_files"] == ["file-a.woff2", "file-b.woff2"]
    assert manifest["installed_version"] == "firasans:v18"
    assert manifest["latest_version"] == "firasans:v18"
    assert manifest["check_interval_days"] == frontend_assets.DEFAULT_FONT_CHECK_DAYS


def test_font_notify_mode_keeps_pinned_assets_and_marks_update_available(tmp_path, monkeypatch) -> None:
    _configure_font_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("G3KU_FRONTEND_FONT_UPDATE_MODE", "notify")
    monkeypatch.delenv("G3KU_FRONTEND_ASSET_UPDATE_MODE", raising=False)

    original_css = "/* local */\n@font-face { src: url('./font-a.woff2'); }\n"
    (tmp_path / "google-fonts.css").write_text(original_css, encoding="utf-8")
    (tmp_path / "font-a.woff2").write_bytes(b"old-font")
    checked_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    (tmp_path / "google-fonts-manifest.json").write_text(
        json.dumps(
            {
                "installed_version": "firasans:v17",
                "installed_revision": "old-revision",
                "font_files": ["font-a.woff2"],
                "checked_at": checked_at,
                "installed_at": checked_at,
            }
        ),
        encoding="utf-8",
    )

    latest_stylesheet = "@font-face { src: url(https://fonts.gstatic.com/s/firasans/v18/file-b.woff2); }\n"
    monkeypatch.setattr(frontend_assets, "_fetch_text", lambda url, *, headers: latest_stylesheet)

    updated = frontend_assets.ensure_frontend_font_assets(force_refresh=True)

    assert updated is False
    assert (tmp_path / "google-fonts.css").read_text(encoding="utf-8") == original_css
    assert (tmp_path / "font-a.woff2").read_bytes() == b"old-font"

    manifest = json.loads((tmp_path / "google-fonts-manifest.json").read_text(encoding="utf-8"))
    assert manifest["installed_version"] == "firasans:v17"
    assert manifest["latest_version"] == "firasans:v18"
    assert manifest["update_available"] is True
    assert manifest["update_mode"] == "notify"


def test_lucide_notify_mode_marks_update_available_without_replacing_bundle(tmp_path, monkeypatch) -> None:
    _configure_lucide_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("G3KU_FRONTEND_LUCIDE_UPDATE_MODE", "notify")
    monkeypatch.delenv("G3KU_FRONTEND_ASSET_UPDATE_MODE", raising=False)

    (tmp_path / "lucide.min.js").write_text("old bundle", encoding="utf-8")
    checked_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    (tmp_path / "lucide-manifest.json").write_text(
        json.dumps(
            {
                "installed_version": "1.7.0",
                "checked_at": checked_at,
                "installed_at": checked_at,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        frontend_assets,
        "_fetch_lucide_release",
        lambda: frontend_assets.LucideRelease(version="1.8.0", tarball_url="https://example.test/lucide.tgz"),
    )

    updated = frontend_assets.ensure_lucide_asset(force_refresh=True)

    assert updated is False
    assert (tmp_path / "lucide.min.js").read_text(encoding="utf-8") == "old bundle"
    manifest = json.loads((tmp_path / "lucide-manifest.json").read_text(encoding="utf-8"))
    assert manifest["installed_version"] == "1.7.0"
    assert manifest["latest_version"] == "1.8.0"
    assert manifest["update_available"] is True
    assert manifest["update_mode"] == "notify"


def test_lucide_auto_mode_replaces_bundle_and_manifest(tmp_path, monkeypatch) -> None:
    _configure_lucide_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("G3KU_FRONTEND_LUCIDE_UPDATE_MODE", "auto")
    monkeypatch.delenv("G3KU_FRONTEND_ASSET_UPDATE_MODE", raising=False)

    (tmp_path / "lucide.min.js").write_text("old bundle", encoding="utf-8")
    checked_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    (tmp_path / "lucide-manifest.json").write_text(
        json.dumps(
            {
                "installed_version": "1.7.0",
                "checked_at": checked_at,
                "installed_at": checked_at,
            }
        ),
        encoding="utf-8",
    )
    tarball_bytes = _make_lucide_tarball("new bundle")
    monkeypatch.setattr(
        frontend_assets,
        "_fetch_lucide_release",
        lambda: frontend_assets.LucideRelease(version="1.8.0", tarball_url="https://example.test/lucide.tgz"),
    )
    monkeypatch.setattr(frontend_assets, "_fetch_bytes", lambda url, *, headers: tarball_bytes)

    updated = frontend_assets.ensure_lucide_asset(force_refresh=True)

    assert updated is True
    assert (tmp_path / "lucide.min.js").read_text(encoding="utf-8") == "new bundle"
    manifest = json.loads((tmp_path / "lucide-manifest.json").read_text(encoding="utf-8"))
    assert manifest["installed_version"] == "1.8.0"
    assert manifest["latest_version"] == "1.8.0"
    assert manifest["update_available"] is False
    assert manifest["update_mode"] == "auto"
