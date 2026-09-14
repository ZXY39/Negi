"""轮边界快照 sidecar、转录截断与 Fork 的文件级行为测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime import web_ceo_history_edit as history_edit
from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.session.manager import SessionManager


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(history_edit, "workspace_path", lambda: tmp_path)
    return tmp_path


def _payload(baseline: list[dict] | None = None) -> dict:
    return {
        "frontdoor_request_body_messages": baseline if baseline is not None else [{"role": "user", "content": "hi"}],
        "frontdoor_history_shrink_reason": "user_edit_truncation",
        "frontdoor_stage_state": {"stages": []},
        "frontdoor_canonical_context": {"stages": []},
        "source_reason": "finalize",
    }


def _user(turn_id: str, content: str = "u", ts: str | None = None, **metadata) -> dict:
    return {
        "role": "user",
        "content": content,
        "timestamp": ts or f"2026-09-14T10:00:0{turn_id}",
        "metadata": {"_transcript_turn_id": turn_id, **metadata},
    }


def _assistant(turn_id: str, content: str = "a", ts: str | None = None, **metadata) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "timestamp": ts or f"2026-09-14T10:01:0{turn_id}",
        "turn_id": turn_id,
        "metadata": dict(metadata),
    }


def _make_session(manager: SessionManager, key: str, messages: list[dict]):
    session = manager.get_or_create(key)
    for message in messages:
        session.messages.append(dict(message))
    session.metadata = {"title": "测试会话", "last_preview_text": "old"}
    manager.save(session)
    return session


# ---------- 轮边界快照 sidecar ----------


def test_turn_boundary_snapshot_roundtrip_gzip_and_turn_id(workspace):
    wcs.write_turn_boundary_snapshot("web:ceo-x", "turn1", _payload())
    directory = wcs.turn_boundary_dir_for_session("web:ceo-x", create=False)
    files = list(directory.glob("*"))
    assert [f.name for f in files] == ["turn1.json.gz"]
    restored = wcs.read_turn_boundary_snapshot("web:ceo-x", "turn1")
    assert restored is not None
    assert restored["frontdoor_request_body_messages"] == [{"role": "user", "content": "hi"}]
    assert wcs.list_turn_boundary_snapshot_turn_ids("web:ceo-x") == {"turn1"}


def test_turn_boundary_snapshot_upsert_and_prune_keeps_three(workspace):
    directory = wcs.turn_boundary_dir_for_session("web:ceo-x")
    for index in range(5):
        wcs.write_turn_boundary_snapshot("web:ceo-x", f"turn{index}", _payload())
        # 显式拉开 mtime,避免文件系统时间粒度导致修剪顺序不稳定。
        path = directory / f"turn{index}.json.gz"
        os.utime(path, (1_700_000_000 + index * 60, 1_700_000_000 + index * 60))
        wcs._prune_turn_boundary_snapshots(directory)
    remaining = wcs.list_turn_boundary_snapshot_turn_ids("web:ceo-x")
    assert remaining == {"turn2", "turn3", "turn4"}


def test_turn_boundary_snapshot_normalizes_shrink_reason_whitelist(workspace):
    payload = _payload()
    payload["frontdoor_history_shrink_reason"] = "bogus_reason"
    payload["source_reason"] = "bogus_source"
    wcs.write_turn_boundary_snapshot("web:ceo-x", "turn1", payload)
    restored = wcs.read_turn_boundary_snapshot("web:ceo-x", "turn1")
    # 白名单外的值被规范化为空(user_edit_truncation 已入白名单,不会被洗掉)。
    assert restored["frontdoor_history_shrink_reason"] == ""
    assert restored["source_reason"] == ""
    payload2 = _payload()
    wcs.write_turn_boundary_snapshot("web:ceo-x", "turn2", payload2)
    restored2 = wcs.read_turn_boundary_snapshot("web:ceo-x", "turn2")
    assert restored2["frontdoor_history_shrink_reason"] == "user_edit_truncation"


def test_clear_turn_boundary_snapshots_selective_and_full(workspace):
    wcs.write_turn_boundary_snapshot("web:ceo-x", "t1", _payload())
    wcs.write_turn_boundary_snapshot("web:ceo-x", "t2", _payload())
    wcs.clear_turn_boundary_snapshots("web:ceo-x", ["t1"])
    assert wcs.list_turn_boundary_snapshot_turn_ids("web:ceo-x") == {"t2"}
    wcs.clear_turn_boundary_snapshots("web:ceo-x")
    assert wcs.list_turn_boundary_snapshot_turn_ids("web:ceo-x") == set()


# ---------- 转录截断 ----------


def _truncation_env(workspace, *, task_ids_on_first_reply: bool = False):
    manager = SessionManager(workspace)
    key = "web:ceo-trunc"
    messages = [
        _user("t1", "第一条"),
        _assistant("t1", "回复一", **({"task_ids": ["task:zzz"]} if task_ids_on_first_reply else {})),
        _user("t2", "第二条"),
        _assistant("t2", "回复二"),
        _user("t3", "第三条"),
    ]
    session = _make_session(manager, key, messages)
    # prev_turn(t1 回复轮)的边界快照 = 截断后的连续性状态。
    wcs.write_turn_boundary_snapshot(key, "t1", _payload([{"role": "user", "content": "第一条"}]))
    # 被截断轮与保留轮各放一份 actual-request artifact。
    request_dir = wcs.actual_request_dir_for_session(key)
    for turn in ("t1", "t2", "t3"):
        (request_dir / f"20260914_{turn}.json").write_text(
            json.dumps({"turn_id": turn, "request_messages": []}), encoding="utf-8"
        )
    # inflight/paused sidecar 存在时应被清理。
    wcs.write_inflight_turn_snapshot(key, {"turn_id": "t3", "status": "running"})
    wcs.write_paused_execution_context(key, {"turn_id": "t2", "status": "paused"})
    return manager, key, session


def test_truncate_rewrites_transcript_and_continuity(workspace):
    manager, key, session = _truncation_env(workspace)
    applied: list[tuple] = []
    runtime_session = SimpleNamespace(
        apply_history_truncation_state=lambda payload, *, removed_turn_ids=None: applied.append((payload, removed_turn_ids)),
    )
    runtime_manager = SimpleNamespace(get=lambda _key: runtime_session)

    result = history_edit.truncate_web_ceo_session_history(
        session_manager=manager,
        runtime_manager=runtime_manager,
        agent=None,
        session_id=key,
        turn_id="t2",
    )
    assert result["continuity_source"] == "turn_boundary"
    assert result["removed_message_count"] == 3
    assert result["removed_turn_ids"] == ["t2", "t3"]

    # 转录只剩 t1 两条,jsonl 原子重写为单 metadata 行 + 消息行。
    raw_lines = [
        json.loads(line)
        for line in manager.get_path(key).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    metadata_rows = [row for row in raw_lines if row.get("_type") == "metadata"]
    assert len(metadata_rows) == 1
    assert metadata_rows[0]["last_user_turn_at"] == session.messages[0]["timestamp"]
    assert metadata_rows[0]["commit_turn_counter"] == 1
    bodies = [row for row in raw_lines if row.get("_type") != "metadata"]
    assert [row["role"] for row in bodies] == ["user", "assistant"]

    # 连续性 sidecar = 边界快照内容,收缩原因标记为 user_edit_truncation。
    sidecar = wcs.read_completed_continuity_snapshot(key)
    assert sidecar["frontdoor_request_body_messages"] == [{"role": "user", "content": "第一条"}]
    assert sidecar["frontdoor_history_shrink_reason"] == "user_edit_truncation"
    assert sidecar["source_reason"] == "user_edit_truncation"

    # inflight/paused 清理;被截轮 artifact 删除,保留轮 artifact 不动。
    assert wcs.read_inflight_turn_snapshot(key) is None
    assert wcs.read_paused_execution_context(key) is None
    remaining_artifacts = {p.name for p in wcs.actual_request_dir_for_session(key, create=False).glob("*.json")}
    assert remaining_artifacts == {"20260914_t1.json"}

    # 内存会话对象就地变更,携带 removed_turn_ids。
    assert len(applied) == 1
    assert applied[0][1] == ["t2", "t3"]


def test_truncate_first_message_is_fresh_path_and_wipes_artifacts(workspace):
    manager, key, _session = _truncation_env(workspace)
    result = history_edit.truncate_web_ceo_session_history(
        session_manager=manager,
        runtime_manager=SimpleNamespace(get=lambda _key: None),
        agent=None,
        session_id=key,
        turn_id="t1",
    )
    assert result["continuity_source"] == "fresh_path"
    assert result["removed_message_count"] == 5
    assert list(manager.get_or_create(key).messages) == []
    sidecar = wcs.read_completed_continuity_snapshot(key)
    assert sidecar["frontdoor_request_body_messages"] == []
    # 空基线 sidecar 挡不住重启 artifact 兜底:整目录必须清空。
    request_dir = wcs.actual_request_dir_for_session(key, create=False)
    assert not request_dir.exists() or not list(request_dir.glob("*.json"))


def test_truncate_blocked_by_task_gate(workspace):
    manager, key, _session = _truncation_env(workspace, task_ids_on_first_reply=True)
    with pytest.raises(history_edit.HistoryEditError) as excinfo:
        history_edit.truncate_web_ceo_session_history(
            session_manager=manager,
            runtime_manager=SimpleNamespace(get=lambda _key: None),
            agent=None,
            session_id=key,
            turn_id="t2",
        )
    assert excinfo.value.code == "edit_fork_blocked_by_async_task"
    assert excinfo.value.status_code == 409
    # 拒绝时转录不动。
    assert len(manager.get_or_create(key).messages) == 5


def test_truncate_without_boundary_snapshot_is_rejected(workspace):
    manager, key, _session = _truncation_env(workspace)
    wcs.clear_turn_boundary_snapshots(key)
    with pytest.raises(history_edit.HistoryEditError) as excinfo:
        history_edit.truncate_web_ceo_session_history(
            session_manager=manager,
            runtime_manager=SimpleNamespace(get=lambda _key: None),
            agent=None,
            session_id=key,
            turn_id="t2",
        )
    assert excinfo.value.code == "boundary_unavailable"


# ---------- Fork ----------


def test_fork_copies_prefix_uploads_and_continuity(workspace):
    manager = SessionManager(workspace)
    key = "web:ceo-src"
    # 源会话上传目录与一个真实文件。
    src_upload = wcs.upload_dir_for_session(key)
    src_file = src_upload / "abc_photo.png"
    src_file.write_bytes(b"png-bytes")
    descriptor = {
        "name": "photo.png",
        "path": str(src_file.resolve()),
        "relative_path": src_file.resolve().relative_to(workspace).as_posix(),
        "mime_type": "image/png",
        "size": src_file.stat().st_size,
        "kind": "image",
    }
    note = f"Uploaded attachments:\n- image: photo.png (local path: {descriptor['path']})"
    first_user = _user("t1", f"看这张图\n\n{note}", web_ceo_uploads=[descriptor], web_ceo_raw_text="看这张图")
    first_user["attachments"] = [descriptor["path"]]
    messages = [
        first_user,
        _assistant("t1", "已收到图片"),
        _user("t2", "第二条消息"),
    ]
    _make_session(manager, key, messages)
    wcs.write_turn_boundary_snapshot(key, "t1", _payload([{"role": "user", "content": note}]))
    source_jsonl_before = manager.get_path(key).read_text(encoding="utf-8")

    result = history_edit.fork_web_ceo_session(
        session_manager=manager,
        agent=None,
        session_id=key,
        turn_id="t2",
    )
    new_key = result["session_id"]
    assert new_key != key
    assert result["copied_message_count"] == 2
    assert result["continuity_source"] == "turn_boundary"
    # composer 预填 = 被点击消息原文。
    assert result["composer"]["text"] == "第二条消息"
    assert result["composer"]["uploads"] == []

    # 新会话转录 = 前缀复制,附件描述符与 content 路径已重写到新上传目录。
    new_session = manager.get_or_create(new_key)
    copied_user = new_session.messages[0]
    new_descriptor = copied_user["metadata"]["web_ceo_uploads"][0]
    assert new_descriptor["path"] != descriptor["path"]
    assert Path(new_descriptor["path"]).exists()
    assert str(wcs.upload_dir_for_session(new_key, create=False)) in new_descriptor["path"]
    assert copied_user["attachments"] == [new_descriptor["path"]]
    assert new_descriptor["path"] in copied_user["content"]
    assert descriptor["path"] not in copied_user["content"]
    # 原文/元数据保真。
    assert copied_user["metadata"]["web_ceo_raw_text"] == "看这张图"
    assert new_session.metadata["title"] == "测试会话 · Fork"
    assert new_session.commit_turn_counter == 1

    # 连续性 sidecar 写入新会话,基线内的路径也已重写。
    sidecar = wcs.read_completed_continuity_snapshot(new_key)
    assert sidecar is not None
    assert sidecar["frontdoor_history_shrink_reason"] == "user_edit_truncation"
    assert new_descriptor["path"] in sidecar["frontdoor_request_body_messages"][0]["content"]
    # prev_turn 边界快照复制到新会话(尾部可再编辑/再 Fork)。
    assert wcs.read_turn_boundary_snapshot(new_key, "t1") is not None

    # 源会话零变更。
    assert manager.get_path(key).read_text(encoding="utf-8") == source_jsonl_before
    assert src_file.exists()


def test_fork_clicked_message_uploads_returned_as_composer(workspace):
    manager = SessionManager(workspace)
    key = "web:ceo-src2"
    src_upload = wcs.upload_dir_for_session(key)
    src_file = src_upload / "def_report.pdf"
    src_file.write_bytes(b"pdf-bytes")
    descriptor = {
        "name": "report.pdf",
        "path": str(src_file.resolve()),
        "relative_path": src_file.resolve().relative_to(workspace).as_posix(),
        "mime_type": "application/pdf",
        "size": src_file.stat().st_size,
        "kind": "file",
    }
    messages = [
        _user("t1", "第一条"),
        _assistant("t1", "回复"),
        _user("t2", "处理这个文件", web_ceo_uploads=[descriptor], web_ceo_raw_text="处理这个文件"),
    ]
    _make_session(manager, key, messages)
    wcs.write_turn_boundary_snapshot(key, "t1", _payload())

    result = history_edit.fork_web_ceo_session(
        session_manager=manager, agent=None, session_id=key, turn_id="t2",
    )
    composer = result["composer"]
    assert composer["text"] == "处理这个文件"
    assert len(composer["uploads"]) == 1
    copied_path = composer["uploads"][0]["path"]
    assert copied_path != descriptor["path"]
    assert Path(copied_path).exists()
    assert str(wcs.upload_dir_for_session(result["session_id"], create=False)) in copied_path
    # 被点击消息不进新转录。
    new_session = manager.get_or_create(result["session_id"])
    assert len(new_session.messages) == 2


# ---------- apply_history_truncation_state 两分支 ----------


def _truncation_stub():
    from g3ku.runtime.session_agent import RuntimeAgentSession

    calls: list[dict] = []

    class _State:
        def __init__(self):
            self.queued_follow_up_messages = ["stale"]
            self.messages = ["stale-agent-message"]

    stub = SimpleNamespace()
    stub._state = _State()
    stub._frontdoor_request_body_messages = [{"role": "user", "content": "stale"}]
    stub._frontdoor_history_shrink_reason = "stale"
    stub._frontdoor_pending_shrink_reason = "stale"
    stub._frontdoor_token_preflight_diagnostics = {"x": 1}
    stub._frontdoor_actual_request_path = "stale.json"
    stub._frontdoor_actual_request_history = [{"a": 1}]
    stub._frontdoor_stage_state = {"stages": [1]}
    stub._frontdoor_canonical_context = {"stages": [1]}
    stub._compression_state = {"c": 1}
    stub._semantic_context_state = {"s": 1}
    stub._frontdoor_model_retry_status = {"r": 1}
    stub._frontdoor_prompt_cache_key_hash = "h"
    stub._frontdoor_actual_request_hash = "h"
    stub._frontdoor_actual_request_message_count = 3
    stub._frontdoor_actual_tool_schema_hash = "h"
    stub._frontdoor_restore_source = "completed_continuity"
    stub._frontdoor_baseline_sync_decision = "allowed"
    stub._frontdoor_token_compression_applied_turn = True
    stub._frontdoor_previous_actual_request_path = "p.json"
    stub._frontdoor_previous_actual_request_history = [{"b": 2}]
    stub._frontdoor_completed_continuity_bridge_pending = True
    stub._frontdoor_selection_debug = {"d": 1}
    stub._frontdoor_repair_required_tool_items = [1]
    stub._frontdoor_repair_required_skill_items = [2]
    stub._frontdoor_turn_usage = {"t1": {"input_tokens": 1}, "t2": {"input_tokens": 2}}
    stub._preserved_inflight_turn = {"turn": 1}
    stub._follow_up_transition_snapshot = {"snap": 1}
    stub._paused_cleared = False
    stub.clear_paused_execution_context = lambda: setattr(stub, "_paused_cleared", True)
    stub._active_turn_id = "t2"
    stub._active_batch_id = "b1"
    stub._active_user_batch_inputs = ["x"]
    stub._clear_user_batch_context = lambda: calls.append("clear_batch")
    stub._last_verified_task_ids = ["task:a"]
    stub._assistant_stream_pending_text = "p"
    stub._assistant_stream_last_emitted_text = "e"
    stub._assistant_segment_open = True
    return RuntimeAgentSession, stub, calls


def test_apply_history_truncation_state_empty_baseline_resets_everything():
    session_cls, stub, calls = _truncation_stub()
    payload = {"frontdoor_request_body_messages": [], "frontdoor_history_shrink_reason": "user_edit_truncation"}
    # 空基线:_restore 返回 False(用 stub 的 lambda 模拟既有行为)。
    stub._restore_frontdoor_state_from_payload = lambda *_a, **_k: False
    session_cls.apply_history_truncation_state(stub, payload, removed_turn_ids=["t2"])
    assert stub._frontdoor_request_body_messages == []
    assert stub._frontdoor_stage_state == {}
    from g3ku.runtime.frontdoor.canonical_context import default_frontdoor_canonical_context

    assert stub._frontdoor_canonical_context == default_frontdoor_canonical_context()
    assert stub._frontdoor_history_shrink_reason == "user_edit_truncation"
    assert stub._frontdoor_restore_source == "none"
    assert stub._frontdoor_actual_request_message_count == 0
    assert stub._state.queued_follow_up_messages == []
    assert stub._state.messages == []
    assert stub._frontdoor_turn_usage == {"t1": {"input_tokens": 1}}  # 只删被截轮
    assert stub._preserved_inflight_turn is None
    assert stub._paused_cleared is True
    assert stub._active_turn_id is None
    assert calls == ["clear_batch"]
    assert stub._frontdoor_previous_actual_request_path == ""
    assert stub._frontdoor_completed_continuity_bridge_pending is False


def test_apply_history_truncation_state_nonempty_baseline_uses_restore_path():
    session_cls, stub, _calls = _truncation_stub()
    restore_calls: list[dict] = []

    def _restore(payload, *, source, allow_continuity_bridge):
        restore_calls.append({"payload": payload, "source": source, "bridge": allow_continuity_bridge})
        stub._frontdoor_request_body_messages = list(payload["frontdoor_request_body_messages"])
        return True

    stub._restore_frontdoor_state_from_payload = _restore
    payload = {"frontdoor_request_body_messages": [{"role": "user", "content": "kept"}]}
    session_cls.apply_history_truncation_state(stub, payload, removed_turn_ids=[])
    assert restore_calls == [{"payload": payload, "source": "completed_continuity", "bridge": False}]
    assert stub._frontdoor_request_body_messages == [{"role": "user", "content": "kept"}]
    # 轮级缓存清理与空基线分支一致。
    assert stub._state.queued_follow_up_messages == []
    assert stub._preserved_inflight_turn is None
    assert stub._paused_cleared is True


# ---------- 每轮边界快照 hook（_sync_completed_continuity_snapshot） ----------


def test_sync_completed_continuity_snapshot_upserts_turn_boundary(workspace):
    from g3ku.runtime.session_agent import RuntimeAgentSession

    stub = SimpleNamespace()
    stub._state = SimpleNamespace(session_key="web:ceo-hook")
    stub._active_turn_id = "turn-h1"
    stub._frontdoor_request_body_messages = [{"role": "user", "content": "基线"}]
    stub._frontdoor_history_shrink_reason = ""
    stub._frontdoor_pending_shrink_reason = ""
    stub._frontdoor_token_preflight_diagnostics = {}
    stub._frontdoor_actual_request_path = ""
    stub._frontdoor_actual_request_history = []
    stub._frontdoor_stage_state = {}
    stub._frontdoor_canonical_context = {}
    stub._compression_state = {}
    stub._semantic_context_state = {}
    stub._frontdoor_hydrated_tool_names = []
    stub._frontdoor_capability_snapshot_exposure_revision = ""
    stub._frontdoor_visible_tool_ids = []
    stub._frontdoor_visible_skill_ids = []
    stub._frontdoor_provider_tool_schema_names = []
    stub._frontdoor_restore_source = "none"
    stub._frontdoor_baseline_sync_decision = ""
    stub._normalized_name_list = lambda values, sort_values=False: list(values or [])

    RuntimeAgentSession._sync_completed_continuity_snapshot(stub, source_reason="finalize")

    # completed continuity sidecar 与边界快照同时落盘,内容一致(边界快照带 turn_id)。
    sidecar = wcs.read_completed_continuity_snapshot("web:ceo-hook")
    boundary = wcs.read_turn_boundary_snapshot("web:ceo-hook", "turn-h1")
    assert sidecar is not None and boundary is not None
    assert boundary["frontdoor_request_body_messages"] == [{"role": "user", "content": "基线"}]
    # 同轮再次写入 = upsert 覆盖,不产生第二份文件。
    stub._frontdoor_request_body_messages = [{"role": "user", "content": "基线"}, {"role": "assistant", "content": "回复"}]
    RuntimeAgentSession._sync_completed_continuity_snapshot(stub, source_reason="finalize")
    assert wcs.list_turn_boundary_snapshot_turn_ids("web:ceo-hook") == {"turn-h1"}
    boundary2 = wcs.read_turn_boundary_snapshot("web:ceo-hook", "turn-h1")
    assert len(boundary2["frontdoor_request_body_messages"]) == 2


def test_sync_completed_continuity_snapshot_without_turn_id_skips_boundary(workspace):
    from g3ku.runtime.session_agent import RuntimeAgentSession

    stub = SimpleNamespace()
    stub._state = SimpleNamespace(session_key="web:ceo-hook2")
    stub._active_turn_id = None
    stub._frontdoor_request_body_messages = [{"role": "user", "content": "x"}]
    for name in (
        "_frontdoor_history_shrink_reason", "_frontdoor_pending_shrink_reason",
        "_frontdoor_actual_request_path", "_frontdoor_capability_snapshot_exposure_revision",
        "_frontdoor_restore_source", "_frontdoor_baseline_sync_decision",
    ):
        setattr(stub, name, "")
    for name in (
        "_frontdoor_token_preflight_diagnostics", "_frontdoor_stage_state",
        "_frontdoor_canonical_context", "_compression_state", "_semantic_context_state",
    ):
        setattr(stub, name, {})
    stub._frontdoor_actual_request_history = []
    stub._frontdoor_hydrated_tool_names = []
    stub._frontdoor_visible_tool_ids = []
    stub._frontdoor_visible_skill_ids = []
    stub._frontdoor_provider_tool_schema_names = []
    stub._normalized_name_list = lambda values, sort_values=False: list(values or [])

    RuntimeAgentSession._sync_completed_continuity_snapshot(stub, source_reason="actual_request_sync")
    assert wcs.read_completed_continuity_snapshot("web:ceo-hook2") is not None
    assert wcs.list_turn_boundary_snapshot_turn_ids("web:ceo-hook2") == set()
