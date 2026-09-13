"""managed-worker.log best-effort 轮转单测。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from g3ku.web.worker_control import (
    _MANAGED_WORKER_LOG_MAX_BYTES,
    _MANAGED_WORKER_LOG_RETENTION_SECONDS,
    _rotate_managed_worker_log_if_needed,
)


def test_rotation_skipped_below_threshold(tmp_path: Path) -> None:
    log = tmp_path / "managed-worker.log"
    log.write_text("small", encoding="utf-8")
    _rotate_managed_worker_log_if_needed(log)
    assert log.exists()
    assert list(tmp_path.glob("managed-worker.log.*")) == []


def test_rotation_renames_over_threshold_and_cleans_old_generations(tmp_path: Path) -> None:
    log = tmp_path / "managed-worker.log"
    log.write_bytes(b"x" * (_MANAGED_WORKER_LOG_MAX_BYTES + 1))
    # 预置一个超期旧代与一个新代
    stale = tmp_path / "managed-worker.log.20200101-000000"
    stale.write_text("old", encoding="utf-8")
    old_ts = time.time() - _MANAGED_WORKER_LOG_RETENTION_SECONDS - 3600
    os.utime(stale, (old_ts, old_ts))
    fresh = tmp_path / "managed-worker.log.20990101-000000"
    fresh.write_text("recent", encoding="utf-8")

    _rotate_managed_worker_log_if_needed(log)

    assert not log.exists(), "超阈值应被 rename 走"
    rotated = [p for p in tmp_path.glob("managed-worker.log.*") if p not in (stale, fresh)]
    assert len(rotated) == 1 and rotated[0].stat().st_size == _MANAGED_WORKER_LOG_MAX_BYTES + 1
    assert not stale.exists(), "7 天以上旧代应被清理"
    assert fresh.exists(), "未超期代保留"


def test_rotation_survives_rename_failure(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "managed-worker.log"
    log.write_bytes(b"x" * (_MANAGED_WORKER_LOG_MAX_BYTES + 1))

    def _boom(self, target):
        raise OSError(13, "being used by another process")

    monkeypatch.setattr(Path, "rename", _boom)
    _rotate_managed_worker_log_if_needed(log)  # 不抛
    assert log.exists(), "rename 失败降级为继续追加（现状行为）"
