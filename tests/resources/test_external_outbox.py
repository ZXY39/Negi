"""Unit tests for the durable external outbox ledger (external_outbox.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from g3ku.runtime import external_outbox


@pytest.fixture(autouse=True)
def outbox_root(tmp_path: Path):
    external_outbox.configure_external_outbox_root(tmp_path)
    yield tmp_path
    external_outbox.configure_external_outbox_root(None)


def test_record_and_load_pending_roundtrip() -> None:
    first = external_outbox.record_outbound_message(
        session_key="ext:qq-official:aaa", external_key="qq:c2c:u1", text="提醒一"
    )
    second = external_outbox.record_outbound_message(
        session_key="ext:qq-official:aaa", external_key="qq:c2c:u1", text="提醒二", dedupe_key="k"
    )
    assert first and second and first != second
    pending = external_outbox.load_pending_outbound()
    assert [item["text"] for item in pending] == ["提醒一", "提醒二"]
    assert pending[1]["dedupe_key"] == "k"
    assert pending[0]["session_key"] == "ext:qq-official:aaa"


def test_ack_removes_from_pending_and_is_session_scoped() -> None:
    outbox_id = external_outbox.record_outbound_message(
        session_key="ext:s1", external_key="qq:c2c:u1", text="x"
    )
    # 会话不匹配：拒绝 ack（REST 面的跨会话防护）。
    assert external_outbox.ack_outbound_message(outbox_id, session_key="ext:other") is False
    assert len(external_outbox.load_pending_outbound()) == 1
    assert external_outbox.ack_outbound_message(outbox_id, session_key="ext:s1") is True
    assert external_outbox.load_pending_outbound() == []
    # 幂等：重复 ack 只是多一条 tombstone，不报错。
    assert external_outbox.ack_outbound_message(outbox_id, session_key="ext:s1") is True


def test_ack_unknown_id_without_session_scope_still_appends() -> None:
    # 无 session_key 的内部调用（如 expire）不做存在性校验，仅追加 tombstone。
    assert external_outbox.ack_outbound_message("obx-missing") is True
    assert external_outbox.load_pending_outbound() == []


def test_expire_stale_pending() -> None:
    external_outbox.record_outbound_message(session_key="ext:s1", external_key="k", text="old")
    fresh = external_outbox.record_outbound_message(session_key="ext:s1", external_key="k", text="fresh")
    # 手工把第一条的 ts 改到 25 小时前。
    path = external_outbox._outbox_path()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines[0]["ts"] = (datetime.now() - timedelta(hours=25)).isoformat()
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in lines), encoding="utf-8"
    )
    assert external_outbox.expire_stale_pending() == 1
    pending = external_outbox.load_pending_outbound()
    assert [item["id"] for item in pending] == [fresh]


def test_compact_keeps_only_pending() -> None:
    first = external_outbox.record_outbound_message(session_key="ext:s1", external_key="k", text="a")
    external_outbox.record_outbound_message(session_key="ext:s1", external_key="k", text="b")
    external_outbox.ack_outbound_message(first, session_key="ext:s1")
    external_outbox.compact_outbox()
    pending = external_outbox.load_pending_outbound()
    assert [item["text"] for item in pending] == ["b"]
    lines = [
        line
        for line in external_outbox._outbox_path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1


def test_record_failure_degrades_to_empty_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """磁盘满（Errno 28）时登记失败返回空 id：调用方仍会走内存投递，不阻断。"""

    def _boom() -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(external_outbox, "_outbox_path", _boom)
    assert (
        external_outbox.record_outbound_message(session_key="s", external_key="k", text="t")
        == ""
    )


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    external_outbox.configure_external_outbox_root(tmp_path / "nowhere")
    assert external_outbox.load_pending_outbound() == []
    assert external_outbox.expire_stale_pending() == 0
    external_outbox.compact_outbox()  # 不存在时静默返回
