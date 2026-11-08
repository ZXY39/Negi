"""Pure-note batches go through the same preview -> confirm gate as memory rows.

Regression for the incident where `batch_has_mutation()` ignored `note_upserts` /
`note_edits`: a persona upsert was staged, refused at confirm as "changes nothing",
resubmitted in a repair loop, and the last (garbage) resubmit landed silently because
the commit gate also read the batch as zero-change. The persona note ended up as a
two-character placeholder and became the model's identity.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _cfg():
    from g3ku.config.schema import MemoryToolsConfig

    return MemoryToolsConfig()


def _manager(tmp_path: Path):
    return _module().MemoryManager(tmp_path, _cfg())


def _batch(module, *, op: str = "write", request_id: str = "req_confirm_gate"):
    return module.MemoryBatch(
        op=op,
        items=[
            module.MemoryQueueRequest(
                op=op,
                decision_source="user",
                payload_text="update the persona note",
                created_at="2026-11-08T20:00:00+08:00",
                request_id=request_id,
            )
        ],
    )


def _session(manager, *, op: str = "write"):
    return _module()._MemoryToolSession(
        snapshot_text=manager.snapshot_text(),
        notes_dir=manager.notes_dir,
        batch_op=op,
    )


def _tools(manager, session) -> dict:
    return {tool.name: tool for tool in manager._memory_agent_tools(session)}


def _validate(manager, session, module):
    return manager._validate_candidate_state(
        batch=_batch(module),
        before_text=manager.snapshot_text(),
        session=session,
    )


def test_pure_note_upsert_is_previewed_and_requires_confirm(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        session = _session(manager)
        tools = _tools(manager, session)

        staged = tools["memory_apply_batch"].invoke(
            {"note_upserts": {"note_persona": "我是鲸鱼娘大肥鱼，Negi 的前台。"}}
        )

        assert staged.get("ok") is True, staged
        assert staged.get("status") == "batch_previewed", staged
        summary = staged["preview"]["notes_changed"]
        assert summary == [
            {"note_ref": "note_persona", "action": "upsert", "chars": summary[0]["chars"]}
        ], staged["preview"]
        assert isinstance(summary[0]["chars"], int) and summary[0]["chars"] > 0
        assert staged["preview"]["adds"] == 0
        assert session.batch_has_mutation() is True

        confirmed = tools["memory_apply_confirm"].invoke({})
        assert confirmed == {"ok": True, "status": "batch_confirmed"}, confirmed
    finally:
        manager.close()


def test_unconfirmed_pure_note_upsert_is_refused_at_commit(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        session = _session(manager)
        staged = session.apply_batch(note_upserts={"note_persona": "完整人设正文"})
        assert staged.get("ok") is True, staged

        with pytest.raises(module._MemoryAgentValidationError, match="memory_apply_confirm is required"):
            _validate(manager, session, module)
    finally:
        manager.close()


def test_pure_note_edits_are_previewed_and_require_confirm(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        note = tmp_path / "memory" / "notes" / "note_ports.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("端口：8788\n", encoding="utf-8")

        session = _session(manager)
        tools = _tools(manager, session)

        staged = tools["memory_apply_batch"].invoke(
            {"note_edits": {"note_ports": [{"old_text": "8788", "new_text": "8799"}]}}
        )

        assert staged.get("ok") is True, staged
        assert staged.get("status") == "batch_previewed", staged
        assert staged["preview"]["notes_changed"] == [
            {"note_ref": "note_ports", "action": "edit", "edits": 1}
        ], staged["preview"]

        with pytest.raises(module._MemoryAgentValidationError, match="memory_apply_confirm is required"):
            _validate(manager, session, module)

        assert tools["memory_apply_confirm"].invoke({}).get("status") == "batch_confirmed"
        _validate(manager, session, module)
    finally:
        manager.close()


def test_zero_change_exits_still_need_no_confirmation(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        session = _session(manager)
        staged = session.apply_batch(noop_reason="nothing worth remembering")
        assert staged.get("ok") is True, staged
        assert session.batch_has_mutation() is False

        refused = session.confirm_batch()
        assert refused.get("ok") is False
        assert "changes nothing" in str(refused.get("errors", {}).get("batch", ""))

        validated = _validate(manager, session, module)
        assert validated.write_mode == ""
    finally:
        manager.close()


def test_resubmit_voids_note_confirmation_and_only_the_last_body_lands(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        session = _session(manager)
        tools = _tools(manager, session)

        first = tools["memory_apply_batch"].invoke({"note_upserts": {"note_persona": "第一版人设"}})
        assert first.get("status") == "batch_previewed", first
        assert tools["memory_apply_confirm"].invoke({}).get("status") == "batch_confirmed"

        second = tools["memory_apply_batch"].invoke({"note_upserts": {"note_persona": "第二版人设"}})
        assert second.get("ok") is True, second
        assert second.get("replaced_previous") is True, second
        assert session.confirm_count == 0, "a resubmission must void the earlier confirmation"

        # The refused-at-commit shape this file exists for: between the resubmit and its
        # own confirm, the candidate is not landable.
        with pytest.raises(module._MemoryAgentValidationError, match="memory_apply_confirm is required"):
            _validate(manager, session, module)

        assert tools["memory_apply_confirm"].invoke({}).get("status") == "batch_confirmed"
        validated = _validate(manager, session, module)
        manager._commit_validated_write(validated)

        landed = (tmp_path / "memory" / "notes" / "note_persona.md").read_text(encoding="utf-8")
        assert "第二版人设" in landed
        assert "第一版人设" not in landed
    finally:
        manager.close()


def test_persona_upsert_lands_through_the_full_gate(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        session = _session(manager)
        tools = _tools(manager, session)

        body = "我是『飞鸟马时』，Negi 的前台助手。"
        staged = tools["memory_apply_batch"].invoke({"note_upserts": {"note_persona": body}})
        assert staged.get("status") == "batch_previewed", staged
        assert tools["memory_apply_confirm"].invoke({}).get("status") == "batch_confirmed"

        validated = _validate(manager, session, module)
        manager._commit_validated_write(validated)

        assert (tmp_path / "memory" / "notes" / "note_persona.md").read_text(encoding="utf-8").strip() == body
    finally:
        manager.close()


def test_tool_envelope_reflects_an_inner_failure(tmp_path: Path) -> None:
    import asyncio

    module = _module()
    manager = _manager(tmp_path)
    try:
        session = _session(manager)
        tool_list = manager._memory_agent_tools(session)

        def _run(name: str, args: dict) -> dict:
            return asyncio.run(
                module.MemoryManager._execute_memory_tool(
                    tool_list,
                    {"id": "call_envelope", "name": name, "args": args},
                )
            )

        # Confirming with nothing staged fails inside; the envelope must say so at the
        # top level instead of wrapping the refusal in ok:true.
        envelope = _run("memory_apply_confirm", {})
        assert envelope.get("ok") is False, envelope
        assert envelope.get("errors", {}).get("batch") == "stage a batch before confirming", envelope

        # A string result (note read) is a success.
        note = tmp_path / "memory" / "notes" / "note_plain.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("正文\n", encoding="utf-8")
        read = _run("memory_read_note", {"ref": "note_plain"})
        assert read.get("ok") is True, read
        assert read.get("result") == "正文\n", read
    finally:
        manager.close()
