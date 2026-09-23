"""发布标签识别：向 origin 远端读一次标签列表，与本地版本比对。

只做读取，不上传任何本地信息（版本号也不外发）。任何失败都返回 None：
没有 git、没有网络或远端不是 GitHub 的设备，只是"查不到"，不该影响调用方。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_TAG_REF = re.compile(r"refs/tags/(v\d+\.\d+\.\d+)$")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


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
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return latest_release_tag(completed.stdout or "")
