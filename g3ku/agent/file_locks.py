"""Cross-platform advisory file locks with optional metadata payloads.

Used by the memory agent runtime (and historically the catalog store) to
serialize concurrent writers on Windows and POSIX without external deps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from g3ku.utils.helpers import ensure_dir


def _try_acquire_file_lock(path: Path, *, metadata: dict[str, object] | None = None) -> Any | None:
    ensure_dir(path.parent)
    handle = path.open("a+", encoding="utf-8")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None

    if metadata:
        handle.seek(0)
        handle.truncate(0)
        handle.write(json.dumps(metadata, ensure_ascii=False))
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    return handle


def _release_file_lock(handle: Any) -> None:
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass
