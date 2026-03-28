from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from loguru import logger

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
VENDOR_DIR = FRONTEND_DIR / "vendor"

FONT_ASSET_LABEL = "Google Fonts (Fira Code / Fira Sans)"
FONT_VENDOR_DIR = VENDOR_DIR / "fonts"
FONT_STYLESHEET_PATH = FONT_VENDOR_DIR / "google-fonts.css"
FONT_MANIFEST_PATH = FONT_VENDOR_DIR / "google-fonts-manifest.json"
GOOGLE_FONTS_CSS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Fira+Code:wght@400;500;600;700&"
    "family=Fira+Sans:wght@300;400;500;600;700&"
    "display=swap"
)
GOOGLE_FONTS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
DEFAULT_FONT_CHECK_DAYS = 30
DEFAULT_FONT_REFRESH_DAYS = DEFAULT_FONT_CHECK_DAYS

LUCIDE_ASSET_LABEL = "Lucide icons"
LUCIDE_PACKAGE_NAME = "lucide"
LUCIDE_DEFAULT_PINNED_VERSION = "1.7.0"
LUCIDE_BUNDLE_PATH = VENDOR_DIR / "lucide.min.js"
LUCIDE_MANIFEST_PATH = VENDOR_DIR / "lucide-manifest.json"
LUCIDE_REGISTRY_URL = f"https://registry.npmjs.org/{LUCIDE_PACKAGE_NAME}/latest"
LUCIDE_TARBALL_URL_TEMPLATE = f"https://registry.npmjs.org/{LUCIDE_PACKAGE_NAME}/-/{LUCIDE_PACKAGE_NAME}" + "-{version}.tgz"
LUCIDE_TARBALL_MEMBER = "package/dist/umd/lucide.min.js"
DEFAULT_LUCIDE_CHECK_DAYS = 7

DEFAULT_ASSET_UPDATE_MODE = "notify"
SUPPORTED_ASSET_UPDATE_MODES = {"off", "notify", "auto"}

_FONT_URL_RE = re.compile(r"url\((https://fonts\.gstatic\.com/[^)]+)\)")
_FONT_VERSION_RE = re.compile(r"https://fonts\.gstatic\.com/s/(?P<family>[^/]+)/(?P<version>v[^/]+)/")
_SYNC_LOCK = RLock()


@dataclass(frozen=True)
class FontPayload:
    css_text: str
    remote_urls: tuple[str, ...]
    revision: str
    version_labels: dict[str, str]

    @property
    def version(self) -> str:
        return _format_font_version(self.version_labels)


@dataclass(frozen=True)
class LucideRelease:
    version: str
    tarball_url: str


def frontend_assets_available() -> bool:
    return frontend_font_assets_available() and frontend_lucide_asset_available()


def ensure_frontend_vendor_assets(*, force_refresh: bool = False) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for asset_id, handler in (
        ("google-fonts", ensure_frontend_font_assets),
        ("lucide", ensure_lucide_asset),
    ):
        try:
            results[asset_id] = handler(force_refresh=force_refresh)
        except Exception as exc:
            logger.warning("frontend asset sync skipped for {}: {}", asset_id, exc)
            results[asset_id] = False
    return results


def frontend_font_assets_available() -> bool:
    manifest = _read_manifest(FONT_MANIFEST_PATH)
    if not FONT_STYLESHEET_PATH.exists():
        return False
    font_files = manifest.get("font_files")
    if not isinstance(font_files, list) or not font_files:
        return False
    return all((FONT_VENDOR_DIR / str(name)).is_file() for name in font_files)


def frontend_font_assets_need_refresh(*, force_refresh: bool = False) -> bool:
    manifest = _read_manifest(FONT_MANIFEST_PATH)
    if force_refresh or _asset_force_check("font", legacy_names=("G3KU_FRONTEND_FONT_REFRESH_FORCE",)):
        return True
    if not frontend_font_assets_available():
        return True
    if _asset_update_mode("font") == "off":
        return False
    return _asset_check_due(
        manifest,
        asset_key="font",
        default_days=DEFAULT_FONT_CHECK_DAYS,
        legacy_day_names=("G3KU_FRONTEND_FONT_REFRESH_DAYS",),
    )


def ensure_frontend_font_assets(*, force_refresh: bool = False) -> bool:
    with _SYNC_LOCK:
        manifest = _read_manifest(FONT_MANIFEST_PATH)
        check_due = frontend_font_assets_need_refresh(force_refresh=force_refresh)
        mode = _asset_update_mode("font")
        changed = False

        if not frontend_font_assets_available():
            payload = _fetch_remote_font_payload()
            _install_font_payload(payload)
            logger.info("frontend asset installed: {} ({})", FONT_ASSET_LABEL, payload.version)
            return True

        if not check_due:
            return False

        payload = _fetch_remote_font_payload()
        checked_at = _now_iso()
        installed_version = str(manifest.get("installed_version") or "")
        installed_revision = str(manifest.get("installed_revision") or "")
        font_files = manifest.get("font_files")
        font_files_list = [str(name) for name in font_files] if isinstance(font_files, list) else []

        if payload.revision == installed_revision and payload.version == installed_version:
            manifest.update(
                {
                    "asset_id": "google-fonts",
                    "asset_label": FONT_ASSET_LABEL,
                    "source_url": GOOGLE_FONTS_CSS_URL,
                    "installed_version": payload.version,
                    "installed_revision": payload.revision,
                    "latest_version": payload.version,
                    "latest_revision": payload.revision,
                    "font_versions": payload.version_labels,
                    "font_files": font_files_list,
                    "stylesheet": FONT_STYLESHEET_PATH.name,
                    "update_available": False,
                    "update_mode": mode,
                    "check_interval_days": _asset_check_days(
                        "font",
                        DEFAULT_FONT_CHECK_DAYS,
                        legacy_names=("G3KU_FRONTEND_FONT_REFRESH_DAYS",),
                    ),
                    "checked_at": checked_at,
                    "installed_at": str(
                        manifest.get("installed_at")
                        or manifest.get("refreshed_at")
                        or checked_at
                    ),
                    "last_error": "",
                }
            )
            _write_manifest(FONT_MANIFEST_PATH, manifest)
            return False

        if mode == "auto":
            _install_font_payload(payload)
            logger.info(
                "frontend asset auto-upgraded: {} ({} -> {})",
                FONT_ASSET_LABEL,
                installed_version or "unknown",
                payload.version,
            )
            return True

        manifest.update(
            {
                "asset_id": "google-fonts",
                "asset_label": FONT_ASSET_LABEL,
                "source_url": GOOGLE_FONTS_CSS_URL,
                "installed_version": installed_version,
                "installed_revision": installed_revision,
                "latest_version": payload.version,
                "latest_revision": payload.revision,
                "font_versions": manifest.get("font_versions") or {},
                "latest_font_versions": payload.version_labels,
                "font_files": font_files_list,
                "stylesheet": FONT_STYLESHEET_PATH.name,
                "update_available": True,
                "update_mode": mode,
                "check_interval_days": _asset_check_days(
                    "font",
                    DEFAULT_FONT_CHECK_DAYS,
                    legacy_names=("G3KU_FRONTEND_FONT_REFRESH_DAYS",),
                ),
                "checked_at": checked_at,
                "installed_at": str(
                    manifest.get("installed_at")
                    or manifest.get("refreshed_at")
                    or checked_at
                ),
                "last_error": "",
            }
        )
        _write_manifest(FONT_MANIFEST_PATH, manifest)
        logger.warning(
            "frontend asset update available: {} (pinned={}, latest={}). "
            "Set G3KU_FRONTEND_FONT_UPDATE_MODE=auto or G3KU_FRONTEND_ASSET_UPDATE_MODE=auto to upgrade automatically.",
            FONT_ASSET_LABEL,
            installed_version or "unknown",
            payload.version,
        )
        return changed


def frontend_lucide_asset_available() -> bool:
    manifest = _read_manifest(LUCIDE_MANIFEST_PATH)
    return LUCIDE_BUNDLE_PATH.is_file() and bool(str(manifest.get("installed_version") or "").strip())


def frontend_lucide_asset_need_refresh(*, force_refresh: bool = False) -> bool:
    manifest = _read_manifest(LUCIDE_MANIFEST_PATH)
    if force_refresh or _asset_force_check("lucide"):
        return True
    if not frontend_lucide_asset_available():
        return True
    if _asset_update_mode("lucide") == "off":
        return False
    return _asset_check_due(manifest, asset_key="lucide", default_days=DEFAULT_LUCIDE_CHECK_DAYS)


def ensure_lucide_asset(*, force_refresh: bool = False) -> bool:
    with _SYNC_LOCK:
        manifest = _read_manifest(LUCIDE_MANIFEST_PATH)
        changed = False
        mode = _asset_update_mode("lucide")

        if LUCIDE_BUNDLE_PATH.exists() and not manifest:
            _bootstrap_lucide_manifest()
            manifest = _read_manifest(LUCIDE_MANIFEST_PATH)
            changed = True

        if not LUCIDE_BUNDLE_PATH.exists():
            pinned_version = str(manifest.get("installed_version") or LUCIDE_DEFAULT_PINNED_VERSION).strip()
            release = LucideRelease(
                version=pinned_version,
                tarball_url=LUCIDE_TARBALL_URL_TEMPLATE.format(version=pinned_version),
            )
            _install_lucide_release(release)
            logger.info("frontend asset installed: {} ({})", LUCIDE_ASSET_LABEL, release.version)
            return True

        if not frontend_lucide_asset_need_refresh(force_refresh=force_refresh):
            return changed

        release = _fetch_lucide_release()
        checked_at = _now_iso()
        installed_version = str(manifest.get("installed_version") or LUCIDE_DEFAULT_PINNED_VERSION).strip()

        if release.version == installed_version:
            manifest.update(
                {
                    "asset_id": "lucide",
                    "asset_label": LUCIDE_ASSET_LABEL,
                    "package_name": LUCIDE_PACKAGE_NAME,
                    "bundle": LUCIDE_BUNDLE_PATH.name,
                    "source_url": LUCIDE_REGISTRY_URL,
                    "installed_version": installed_version,
                    "latest_version": release.version,
                    "update_available": False,
                    "update_mode": mode,
                    "check_interval_days": _asset_check_days("lucide", DEFAULT_LUCIDE_CHECK_DAYS),
                    "checked_at": checked_at,
                    "installed_at": str(manifest.get("installed_at") or checked_at),
                    "last_error": "",
                }
            )
            _write_manifest(LUCIDE_MANIFEST_PATH, manifest)
            return changed

        if mode == "auto":
            _install_lucide_release(release)
            logger.info(
                "frontend asset auto-upgraded: {} ({} -> {})",
                LUCIDE_ASSET_LABEL,
                installed_version,
                release.version,
            )
            return True

        manifest.update(
            {
                "asset_id": "lucide",
                "asset_label": LUCIDE_ASSET_LABEL,
                "package_name": LUCIDE_PACKAGE_NAME,
                "bundle": LUCIDE_BUNDLE_PATH.name,
                "source_url": LUCIDE_REGISTRY_URL,
                "installed_version": installed_version,
                "latest_version": release.version,
                "update_available": True,
                "update_mode": mode,
                "check_interval_days": _asset_check_days("lucide", DEFAULT_LUCIDE_CHECK_DAYS),
                "checked_at": checked_at,
                "installed_at": str(manifest.get("installed_at") or checked_at),
                "last_error": "",
            }
        )
        _write_manifest(LUCIDE_MANIFEST_PATH, manifest)
        logger.warning(
            "frontend asset update available: {} (pinned={}, latest={}). "
            "Set G3KU_FRONTEND_LUCIDE_UPDATE_MODE=auto or G3KU_FRONTEND_ASSET_UPDATE_MODE=auto to upgrade automatically.",
            LUCIDE_ASSET_LABEL,
            installed_version,
            release.version,
        )
        return changed


def _bootstrap_lucide_manifest() -> None:
    checked_at = _now_iso()
    manifest = {
        "asset_id": "lucide",
        "asset_label": LUCIDE_ASSET_LABEL,
        "package_name": LUCIDE_PACKAGE_NAME,
        "bundle": LUCIDE_BUNDLE_PATH.name,
        "source_url": LUCIDE_REGISTRY_URL,
        "installed_version": LUCIDE_DEFAULT_PINNED_VERSION,
        "latest_version": LUCIDE_DEFAULT_PINNED_VERSION,
        "update_available": False,
        "update_mode": _asset_update_mode("lucide"),
        "check_interval_days": _asset_check_days("lucide", DEFAULT_LUCIDE_CHECK_DAYS),
        "checked_at": checked_at,
        "installed_at": checked_at,
        "last_error": "",
    }
    _write_manifest(LUCIDE_MANIFEST_PATH, manifest)


def _install_font_payload(payload: FontPayload) -> None:
    checked_at = _now_iso()
    FONT_VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    previous_font_files = {
        path.name
        for path in FONT_VENDOR_DIR.glob("*.woff2")
        if path.is_file()
    }

    with tempfile.TemporaryDirectory(prefix="g3ku-font-sync-", dir=str(FONT_VENDOR_DIR.parent)) as temp_dir:
        temp_root = Path(temp_dir) / "fonts"
        temp_root.mkdir(parents=True, exist_ok=True)
        local_names: dict[str, str] = {}
        used_names: set[str] = set()

        for remote_url in payload.remote_urls:
            local_name = _local_font_filename(remote_url, used_names)
            local_names[remote_url] = local_name
            font_bytes = _fetch_bytes(
                remote_url,
                headers={
                    "User-Agent": GOOGLE_FONTS_USER_AGENT,
                    "Referer": GOOGLE_FONTS_CSS_URL,
                },
            )
            (temp_root / local_name).write_bytes(font_bytes)

        local_css = _FONT_URL_RE.sub(
            lambda match: f"url('./{local_names[match.group(1)]}')",
            payload.css_text,
        ).strip()
        stylesheet = (
            "/* Generated automatically from Google Fonts. */\n"
            f"/* Source: {GOOGLE_FONTS_CSS_URL} */\n"
            f"/* Refreshed at: {checked_at} */\n\n"
            f"{local_css}\n"
        )
        (temp_root / FONT_STYLESHEET_PATH.name).write_text(
            stylesheet,
            encoding="utf-8",
            newline="\n",
        )

        manifest = {
            "asset_id": "google-fonts",
            "asset_label": FONT_ASSET_LABEL,
            "source_url": GOOGLE_FONTS_CSS_URL,
            "stylesheet": FONT_STYLESHEET_PATH.name,
            "installed_version": payload.version,
            "installed_revision": payload.revision,
            "latest_version": payload.version,
            "latest_revision": payload.revision,
            "font_versions": payload.version_labels,
            "font_files": sorted(local_names.values()),
            "update_available": False,
            "update_mode": _asset_update_mode("font"),
            "check_interval_days": _asset_check_days(
                "font",
                DEFAULT_FONT_CHECK_DAYS,
                legacy_names=("G3KU_FRONTEND_FONT_REFRESH_DAYS",),
            ),
            "checked_at": checked_at,
            "installed_at": checked_at,
            "last_error": "",
        }
        _write_manifest(temp_root / FONT_MANIFEST_PATH.name, manifest)

        for artifact in temp_root.iterdir():
            shutil.copy2(artifact, FONT_VENDOR_DIR / artifact.name)

    stale_font_files = previous_font_files - set(manifest["font_files"])
    for stale_name in stale_font_files:
        stale_path = FONT_VENDOR_DIR / stale_name
        try:
            stale_path.unlink()
        except FileNotFoundError:
            pass


def _install_lucide_release(release: LucideRelease) -> None:
    checked_at = _now_iso()
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="g3ku-lucide-sync-", dir=str(VENDOR_DIR)) as temp_dir:
        temp_root = Path(temp_dir)
        tarball_bytes = _fetch_bytes(release.tarball_url, headers={"User-Agent": GOOGLE_FONTS_USER_AGENT})
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as archive:
            try:
                member = archive.getmember(LUCIDE_TARBALL_MEMBER)
            except KeyError as exc:
                raise RuntimeError(f"lucide bundle not found in tarball: {LUCIDE_TARBALL_MEMBER}") from exc
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError("lucide tarball member could not be read")
            (temp_root / LUCIDE_BUNDLE_PATH.name).write_bytes(extracted.read())

        manifest = {
            "asset_id": "lucide",
            "asset_label": LUCIDE_ASSET_LABEL,
            "package_name": LUCIDE_PACKAGE_NAME,
            "bundle": LUCIDE_BUNDLE_PATH.name,
            "source_url": LUCIDE_REGISTRY_URL,
            "installed_version": release.version,
            "latest_version": release.version,
            "update_available": False,
            "update_mode": _asset_update_mode("lucide"),
            "check_interval_days": _asset_check_days("lucide", DEFAULT_LUCIDE_CHECK_DAYS),
            "checked_at": checked_at,
            "installed_at": checked_at,
            "last_error": "",
        }
        _write_manifest(temp_root / LUCIDE_MANIFEST_PATH.name, manifest)

        shutil.copy2(temp_root / LUCIDE_BUNDLE_PATH.name, LUCIDE_BUNDLE_PATH)
        shutil.copy2(temp_root / LUCIDE_MANIFEST_PATH.name, LUCIDE_MANIFEST_PATH)


def _fetch_remote_font_payload() -> FontPayload:
    css_text = _fetch_text(
        GOOGLE_FONTS_CSS_URL,
        headers={"User-Agent": GOOGLE_FONTS_USER_AGENT},
    )
    remote_urls = tuple(dict.fromkeys(_FONT_URL_RE.findall(css_text)))
    if not remote_urls:
        raise RuntimeError("google_fonts_stylesheet_missing_font_urls")
    version_labels = _extract_font_versions(css_text)
    revision = hashlib.sha256(css_text.encode("utf-8")).hexdigest()
    return FontPayload(
        css_text=css_text,
        remote_urls=remote_urls,
        revision=revision,
        version_labels=version_labels,
    )


def _fetch_lucide_release() -> LucideRelease:
    payload = _fetch_json(LUCIDE_REGISTRY_URL)
    version = str(payload.get("version") or "").strip()
    tarball_url = str(payload.get("dist", {}).get("tarball") or "").strip()
    if not version or not tarball_url:
        raise RuntimeError("lucide registry payload missing version or tarball")
    return LucideRelease(version=version, tarball_url=tarball_url)


def _extract_font_versions(css_text: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for match in _FONT_VERSION_RE.finditer(css_text):
        family = match.group("family").strip().lower()
        version = match.group("version").strip().lower()
        if family:
            versions[family] = version
    return dict(sorted(versions.items()))


def _format_font_version(version_labels: dict[str, str]) -> str:
    if not version_labels:
        return "unknown"
    return ",".join(f"{family}:{version}" for family, version in sorted(version_labels.items()))


def _asset_update_mode(asset_key: str) -> str:
    normalized = asset_key.strip().upper().replace("-", "_")
    candidates = [
        f"G3KU_FRONTEND_{normalized}_UPDATE_MODE",
        "G3KU_FRONTEND_ASSET_UPDATE_MODE",
    ]
    for name in candidates:
        raw = str(os.getenv(name, "")).strip().lower()
        if not raw:
            continue
        if raw in SUPPORTED_ASSET_UPDATE_MODES:
            return raw
    return DEFAULT_ASSET_UPDATE_MODE


def _asset_force_check(asset_key: str, *, legacy_names: tuple[str, ...] = ()) -> bool:
    normalized = asset_key.strip().upper().replace("-", "_")
    names = (
        f"G3KU_FRONTEND_{normalized}_FORCE_CHECK",
        "G3KU_FRONTEND_ASSET_FORCE_CHECK",
        *legacy_names,
    )
    return any(_env_flag(name) for name in names)


def _asset_check_days(
    asset_key: str,
    default_days: int,
    *,
    legacy_names: tuple[str, ...] = (),
) -> int:
    normalized = asset_key.strip().upper().replace("-", "_")
    names = (
        f"G3KU_FRONTEND_{normalized}_CHECK_DAYS",
        "G3KU_FRONTEND_ASSET_CHECK_DAYS",
        *legacy_names,
    )
    for name in names:
        raw = str(os.getenv(name, "")).strip()
        if not raw:
            continue
        try:
            return max(1, int(raw))
        except ValueError:
            continue
    return default_days


def _asset_check_due(
    manifest: dict[str, object],
    *,
    asset_key: str,
    default_days: int,
    legacy_day_names: tuple[str, ...] = (),
) -> bool:
    checked_at = _parse_iso8601(manifest.get("checked_at") or manifest.get("refreshed_at"))
    if checked_at is None:
        return True
    interval_days = _asset_check_days(asset_key, default_days, legacy_names=legacy_day_names)
    return datetime.now(UTC) - checked_at >= timedelta(days=interval_days)


def _read_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _local_font_filename(remote_url: str, used_names: set[str]) -> str:
    parsed = urlparse(remote_url)
    base_name = Path(parsed.path).name or "font.woff2"
    candidate = base_name
    stem = Path(base_name).stem or "font"
    suffix = Path(base_name).suffix or ".woff2"
    counter = 2
    while candidate in used_names:
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    used_names.add(candidate)
    return candidate


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_flag(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _parse_iso8601(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _fetch_json(url: str) -> dict[str, object]:
    text = _fetch_text(url, headers={"User-Agent": GOOGLE_FONTS_USER_AGENT})
    payload = json.loads(text)
    return payload if isinstance(payload, dict) else {}


def _fetch_text(url: str, *, headers: dict[str, str]) -> str:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset("utf-8")
        return response.read().decode(charset, errors="strict")


def _fetch_bytes(url: str, *, headers: dict[str, str]) -> bytes:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20) as response:
        return response.read()
