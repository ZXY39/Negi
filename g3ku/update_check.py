"""发布标签识别与检查台账。

远端读取只做 `git ls-remote --tags`，不上传任何本地信息（版本号也不外发）。
任何失败都返回 None / 落一条 error 台账：没有 git、没有网络或远端不是 GitHub
的设备，只是"查不到"，不该影响调用方，也绝不被渲染成"已是最新"。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

_TAG_REF = re.compile(r"refs/tags/(v\d+\.\d+\.\d+)$")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
LEDGER_FILE = 'update-check.json'
DEFAULT_INTERVAL_SECONDS = 5 * 3600.0
REMOTE_UNREACHABLE = 'remote_unreachable'


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(text.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def latest_release_tag(ls_remote_output: str) -> str | None:
    """Pick the highest semver tag from `git ls-remote --tags` stdout.

    Non-releases are filtered by shape: only a bare `refs/tags/vX.Y.Z` counts, so
    `refs/tags/backup/...` and the peeled `^{}` duplicates are skipped.
    """
    best: tuple[int, int, int] | None = None
    best_tag: str | None = None
    for line in ls_remote_output.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        match = _TAG_REF.search(fields[1].strip())
        if not match:
            continue
        tag = match.group(1)
        version = parse_version(tag)
        if version is None:
            continue
        if best is None or version > best:
            best = version
            best_tag = tag
    return best_tag


def fetch_latest_release_tag(project_root: Path, timeout: float = 2.0) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--tags", "origin"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return latest_release_tag(completed.stdout or "")


def _now() -> datetime:
    return datetime.now().astimezone()


def ledger_path() -> Path:
    from g3ku.config.loader import get_data_dir

    return get_data_dir() / LEDGER_FILE


def read_update_ledger(path: Path | None = None) -> dict[str, Any] | None:
    """Last check result, or None when nothing has been checked yet.

    A missing or corrupt ledger is "unknown", which callers must not render as
    "up to date".
    """
    target = path or ledger_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_update_ledger(payload: dict[str, Any], path: Path | None = None) -> None:
    target = path or ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def check_is_due(ledger: dict[str, Any] | None, interval_seconds: float) -> bool:
    if not ledger:
        return True
    checked_at = str(ledger.get("checked_at") or "").strip()
    if not checked_at:
        return True
    try:
        last = datetime.fromisoformat(checked_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.astimezone()
    return (_now() - last).total_seconds() >= max(0.0, float(interval_seconds))


def build_ledger_payload(latest_tag: str | None, *, source: str) -> dict[str, Any]:
    from g3ku import __version__

    current = parse_version(__version__)
    latest = parse_version(latest_tag) if latest_tag else None
    return {
        "checked_at": _now().isoformat(timespec="seconds"),
        "current_version": __version__,
        "latest_tag": latest_tag or "",
        "newer": bool(latest and current and latest > current),
        "source": source,
        "error": "" if latest_tag else REMOTE_UNREACHABLE,
    }


def run_update_check(
    *,
    source: str = "auto",
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    project_root: Path | None = None,
    ledger_file: Path | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Check the remote tags and persist the outcome.

    ``source='auto'`` respects the interval so a periodic caller can run on every
    tick; ``force`` (a manual click) bypasses it. Returns the ledger, or the
    untouched previous ledger when an automatic pass is not due yet.
    """
    root = project_root or Path.cwd()
    target = ledger_file or ledger_path()
    ledger = read_update_ledger(target)
    if source == "auto" and not force and not check_is_due(ledger, interval_seconds):
        return ledger
    latest_tag = fetch_latest_release_tag(root)
    payload = build_ledger_payload(latest_tag, source=source)
    try:
        write_update_ledger(payload, target)
    except OSError:
        return payload
    return payload
