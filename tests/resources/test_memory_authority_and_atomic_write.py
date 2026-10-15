from __future__ import annotations

import contextlib
import importlib
import json
import os
from pathlib import Path
from typing import Any, Iterator

import pytest


def _load_memory_agent_runtime_module():
    assert importlib.util.find_spec("g3ku.agent.memory_agent_runtime") is not None
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _memory_cfg(*, document_max_chars: int = 20000):
    from g3ku.config.schema import MemoryToolsConfig

    payload = MemoryToolsConfig().model_dump(mode="python")
    payload["document"] = {
        "summary_max_chars": 250,
        "document_max_chars": document_max_chars,
        "memory_file": "memory/MEMORY.md",
        "notes_dir": "memory/notes",
    }
    payload["queue"] = {
        "queue_file": "memory/queue.jsonl",
        "ops_file": "memory/ops.jsonl",
        "batch_max_chars": 50,
        "max_wait_seconds": 3,
        "review_interval_turns": 5,
    }
    return MemoryToolsConfig.model_validate(payload)


@contextlib.contextmanager
def _manager(tmp_path: Path, **cfg_kwargs: Any) -> Iterator[Any]:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg(**cfg_kwargs))
    try:
        yield module, manager
    finally:
        manager.close()


def _seed(manager, rows: list[tuple[str, str, str]], *, now_iso: str = "2026-04-17T10:00:00+08:00") -> list[str]:
    manager._memory_repo.apply_mutation(
        adds=[{"content": body, "minimal_memory": minimal, "decision_source": source} for body, minimal, source in rows],
        rewrites=None,
        deletes=None,
        inspired_memory_ids=None,
        now_iso=now_iso,
    )
    return [str(row["memory_id"]) for row in manager._memory_repo.list_memories()]


def _rows(manager) -> list[dict[str, Any]]:
    return manager._memory_repo.list_memories()


def _entry(memory_id: str, body: str, *, source: str = "user", date_text: str = "2026/4/17") -> str:
    return f"---\nid:{memory_id}\n{date_text}-{source}：\n{body}\n"


def _commit(module, manager, *, adds=None, deletes=None, inspired=None, document_text: str = "") -> None:
    manager._commit_validated_write(
        module._MemoryValidatedWrite(
            document_text=document_text,
            note_writes={},
            note_refs_written=[],
            memory_chars_after=len(document_text),
            document_preview=document_text[:120],
            adds=list(adds or []),
            deletes=list(deletes or []),
            inspired_memory_ids=list(inspired or []),
        )
    )


def _add(content: str, minimal: str, source: str = "user") -> dict[str, str]:
    return {"content": content, "minimal_memory": minimal, "decision_source": source}


def test_emptied_mirror_is_rebuilt_without_dropping_sqlite_rows(tmp_path: Path) -> None:
    """A torn or truncated MEMORY.md must never become the authoritative state."""
    with _manager(tmp_path) as (module, manager):
        seeded = _seed(
            manager,
            [("Alpha full rule text", "alpha->min", "user"), ("Beta full rule text", "beta->min", "self")],
        )
        manager.memory_file.write_text("", encoding="utf-8")

        _commit(module, manager, adds=[_add("Gamma new text", "gamma->min")])

        after = {str(row["memory_id"]) for row in _rows(manager)}
        assert set(seeded) <= after, "the emptied mirror wiped rows that only existed in sqlite"
        assert "Alpha full rule text" in manager.snapshot_text()
        assert "Gamma new text" in manager.snapshot_text()


def test_hand_edited_mirror_is_archived_and_not_adopted(tmp_path: Path) -> None:
    with _manager(tmp_path) as (module, manager):
        _seed(manager, [("Authoritative body text", "auth->min", "user")])
        manager.memory_file.write_text(_entry("ZZ9999", "Hand inserted claim"), encoding="utf-8")

        _commit(module, manager, adds=[_add("Newer claim", "new->min")])

        snapshot = manager.snapshot_text()
        assert "Hand inserted claim" not in snapshot
        assert "Authoritative body text" in snapshot
        assert "Newer claim" in snapshot
        assert list((tmp_path / "memory").glob("MEMORY.md.bak.*")), "an external edit must be archived before rebuilding"


def test_compressed_row_keeps_its_uncompressed_body(tmp_path: Path) -> None:
    """Compression changes the rendered form only; sqlite keeps the full text.

    The mismatch below is a drifted date header - the ordinary way a mirror and
    sqlite fall out of step in production. Folding memory_body into the minimal
    string is what the old document-wins reconciliation did with every such case.
    """
    with _manager(tmp_path) as (module, manager):
        _seed(manager, [("A long detailed rule that must survive compression", "long->short", "self")])
        memory_id = str(_rows(manager)[0]["memory_id"])
        assert manager._memory_repo.mark_compressed(memory_id)
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:01+08:00")

        drifted = manager.snapshot_text().replace("260417:", "260416:", 1)
        assert drifted != manager.snapshot_text()
        manager.memory_file.write_text(drifted, encoding="utf-8")

        _commit(module, manager, adds=[_add("Later rule", "later->min")])

        row = next(r for r in _rows(manager) if str(r["memory_id"]) == memory_id)
        assert str(row["memory_body"]) == "A long detailed rule that must survive compression"
        assert str(row["memory_body"]) != str(row["minimal_memory"])


def test_rebuild_keeps_previous_mirror_when_replace_fails(tmp_path: Path, monkeypatch) -> None:
    """A crash between write and rename must not leave a truncated or zero-byte mirror."""
    with _manager(tmp_path) as (module, manager):
        _seed(manager, [("Readable rule text", "read->min", "user")])
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:01+08:00")
        good = manager.memory_file.read_text(encoding="utf-8")
        assert "Readable rule text" in good

        def _boom(src: Any, dst: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:02+08:00")

        assert manager.memory_file.read_text(encoding="utf-8") == good
        leftovers = [p.name for p in (tmp_path / "memory").iterdir() if p.name.startswith("MEMORY.md.")]
        assert not [n for n in leftovers if n.endswith(".tmp")], f"failed atomic write left temp debris: {leftovers}"


def test_over_limit_document_is_rejected_before_sqlite_mutates(tmp_path: Path) -> None:
    """The hard cap is measured on the rendered document. A batch that would bust it
    must leave sqlite and the mirror both untouched - otherwise the mirror keeps
    showing the old document while sqlite has already grown, and the next commit's
    reconciliation rolls the applied batch straight back out of the store."""
    with _manager(tmp_path, document_max_chars=200) as (module, manager):
        _seed(manager, [("Existing text", "exist->min", "user")])
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:01+08:00")
        before_rows = len(_rows(manager))
        before_mirror = manager.snapshot_text()

        with pytest.raises(Exception):
            _commit(module, manager, adds=[_add("x" * 400, "big->min")])

        assert len(_rows(manager)) == before_rows, "sqlite gained a row from a batch the mirror rejected"
        assert manager.snapshot_text() == before_mirror


def test_orphan_mirror_content_is_archived_not_adopted(tmp_path: Path) -> None:
    """Empty sqlite with a populated mirror has no legitimate meaning: `import-legacy`
    requires an *empty* mirror and reads from its own file. So the orphan text is
    archived for the operator, never adopted into the authoritative store."""
    with _manager(tmp_path) as (module, manager):
        manager.memory_file.write_text(_entry("ABc123", "Pre-existing orphan text"), encoding="utf-8")

        _commit(module, manager, adds=[_add("First new rule", "new->min")])

        bodies = {str(row["memory_body"]) for row in _rows(manager)}
        assert "Pre-existing orphan text" not in bodies
        assert "First new rule" in bodies
        archived = list((tmp_path / "memory").glob("MEMORY.md.bak.*"))
        assert archived, "orphan mirror text must be kept as evidence, not silently dropped"
        assert "Pre-existing orphan text" in archived[0].read_text(encoding="utf-8")


def test_import_legacy_still_adopts_the_document(tmp_path: Path) -> None:
    """Legacy import is the single surviving document -> sqlite direction."""
    with _manager(tmp_path) as (module, manager):
        legacy = tmp_path / "legacy_memory.json"
        legacy.write_text(
            json.dumps(
                [
                    {"date_text": "2026/4/17", "source": "user", "summary": "Legacy one"},
                    {"date_text": "2026/4/17", "source": "self", "summary": "Legacy two"},
                ]
            ),
            encoding="utf-8",
        )

        report = manager.import_legacy(str(legacy), apply=True)

        assert report.get("status") == "applied"
        bodies = {str(row["memory_body"]) for row in _rows(manager)}
        assert "Legacy one" in bodies
        assert "Legacy two" in bodies
