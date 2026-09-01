from __future__ import annotations

import json
from datetime import timedelta

from g3ku.session.manager import SessionManager


def _read_lines(path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_append_only_messages_use_incremental_save(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("web:test")
    session.add_message("user", "first")
    manager.save(session)
    path = manager.get_path("web:test")
    first_size = path.stat().st_size

    session.add_message("assistant", "second")
    manager.save(session)

    records = [item for item in _read_lines(path) if item.get("_type") != "metadata"]
    metadata_records = [item for item in _read_lines(path) if item.get("_type") == "metadata"]
    assert [str(item.get("content")) for item in records] == ["first", "second"]
    assert path.stat().st_size > first_size
    assert len(metadata_records) == 2


def test_structural_message_edit_forces_full_rewrite(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("web:test")
    session.add_message("user", "keep")
    session.add_message("assistant", "archive")
    manager.save(session)

    session.messages.pop()
    session.add_message("assistant", "replacement")
    manager.save(session)

    records = [item for item in _read_lines(manager.get_path("web:test")) if item.get("_type") != "metadata"]
    assert [str(item.get("content")) for item in records] == ["keep", "replacement"]


def test_external_file_growth_falls_back_to_full_rewrite(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("web:test")
    session.add_message("user", "first")
    manager.save(session)
    path = manager.get_path("web:test")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "user", "content": "external"}) + "\n")

    session.add_message("assistant", "second")
    manager.save(session)

    records = [item for item in _read_lines(path) if item.get("_type") != "metadata"]
    assert [str(item.get("content")) for item in records] == ["first", "second"]


def test_list_sessions_reads_trailing_incremental_metadata(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("web:test")
    session.add_message("user", "first")
    manager.save(session)
    before_updated_at = session.updated_at

    session.add_message("assistant", "second")
    session.commit_turn_counter = 42
    session.updated_at = before_updated_at + timedelta(seconds=1)
    manager.save(session)

    [listed] = manager.list_sessions()
    assert listed["updated_at"] > before_updated_at.isoformat()
    assert [item for item in _read_lines(manager.get_path("web:test"))][-1]["commit_turn_counter"] == 42


def test_load_projects_oversized_legacy_canonical_context_once(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("web:test")
    stage = {
        "stage_id": "frontdoor-stage-1",
        "stage_index": 1,
        "status": "completed",
        "stage_kind": "normal",
        "created_at": "2026-09-01T00:00:00+08:00",
        "completed_stage_summary": "done",
        "rounds": [
            {
                "round_index": 1,
                "tools": [{"tool_name": "exec", "output_text": "x" * 300_000}],
            }
        ],
    }
    session.messages.append(
        {
            "role": "assistant",
            "content": "reply",
            "canonical_context": {"active_stage_id": "", "stages": [stage]},
        }
    )
    manager.save(session)

    fresh_manager = SessionManager(tmp_path)
    loaded = fresh_manager.get_or_create("web:test")
    before_size = manager.get_path("web:test").stat().st_size
    fresh_manager.save(loaded)
    after_size = manager.get_path("web:test").stat().st_size
    records = [item for item in _read_lines(manager.get_path("web:test")) if item.get("_type") != "metadata"]

    assert after_size < before_size
    assert records[0]["canonical_context_projection"] == "stage_window"
    assert len(json.dumps(records[0]["canonical_context"])) < 10_000


def test_already_projected_records_do_not_repeatedly_force_rewrite(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("web:test")
    stage = {
        "stage_id": "frontdoor-stage-1",
        "stage_index": 1,
        "status": "completed",
        "stage_kind": "normal",
        "created_at": "2026-09-01T00:00:00+08:00",
        "completed_stage_summary": "done",
        "representation": "raw",
        "rounds": [
            {
                "round_index": 1,
                "tools": [{"tool_name": "exec", "output_text": "x" * 300_000}],
            }
        ],
    }
    session.messages.append(
        {
            "role": "assistant",
            "content": "reply",
            "canonical_context": {"active_stage_id": "", "stages": [stage]},
            "canonical_context_projection": "stage_window",
        }
    )
    manager.save(session)
    path = manager.get_path("web:test")

    fresh_manager = SessionManager(tmp_path)
    loaded = fresh_manager.get_or_create("web:test")
    loaded.add_message("user", "next")
    fresh_manager.save(loaded)

    lines = _read_lines(path)
    records = [item for item in lines if item.get("_type") != "metadata"]
    assert [str(item.get("content")) for item in records] == ["reply", "next"]
    # Two metadata rows mean the second save used the append path, not a rewrite.
    assert len([item for item in lines if item.get("_type") == "metadata"]) == 2
    retained = records[0]["canonical_context"]["stages"][0]["rounds"][0]["tools"][0]["output_text"]
    assert len(retained) > 200_000
