"""分块压缩进度是会话状态快照的显式字段，不是从文案里反解的。

前端按 `chunk_index/chunk_count` 现算「上下文分块压缩中（i/x）」；这一层把它们
从运行时会话的 `_compression_state` 原样交给 state/snapshot 帧。单发压缩没有块号，
字段保持缺失，显示侧退回普通「上下文压缩中」。
"""

from __future__ import annotations

from g3ku.runtime.session_agent import RuntimeAgentSession


def _snapshot_for(state: dict[str, object]) -> dict[str, object]:
    session = object.__new__(RuntimeAgentSession)
    session._compression_state = state
    return session._compression_snapshot()


def test_compression_snapshot_preserves_chunk_progress_fields() -> None:
    snapshot = _snapshot_for(
        {
            "status": "running",
            "text": "上下文分块压缩中（2/5）",
            "source": "token_compression",
            "needs_recheck": False,
            "chunk_index": 2,
            "chunk_count": 5,
        }
    )

    assert snapshot == {
        "status": "running",
        "text": "上下文分块压缩中（2/5）",
        "source": "token_compression",
        "needs_recheck": False,
        "chunk_index": 2,
        "chunk_count": 5,
    }


def test_compression_snapshot_keeps_merge_phase_for_restart_recovery() -> None:
    snapshot = _snapshot_for(
        {
            "status": "running",
            "text": "上下文分块压缩中（归并摘要）",
            "source": "manual_context_compression",
            "chunk_index": 3,
            "chunk_count": 3,
            "phase": "merge",
        }
    )

    assert snapshot["chunk_index"] == 3
    assert snapshot["chunk_count"] == 3
    assert snapshot["phase"] == "merge"


def test_compression_snapshot_omits_chunk_progress_when_absent_or_invalid() -> None:
    single_shot = _snapshot_for(
        {
            "status": "running",
            "text": "上下文压缩中",
            "source": "token_compression",
            "needs_recheck": False,
        }
    )
    assert "chunk_index" not in single_shot
    assert "chunk_count" not in single_shot
    assert "phase" not in single_shot

    invalid = _snapshot_for(
        {
            "status": "running",
            "chunk_index": True,
            "chunk_count": "5",
            "phase": "   ",
        }
    )
    assert "chunk_index" not in invalid
    assert "chunk_count" not in invalid
    assert "phase" not in invalid
