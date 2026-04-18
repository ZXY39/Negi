from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner


class _ConsoleStub:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def print(self, *args, **kwargs) -> None:
        _ = kwargs
        self.messages.append(" ".join(str(item) for item in args))

    def print_json(self, payload: str) -> None:
        self.messages.append(str(payload))


def _memory_cli_module():
    return import_module("g3ku.shells.memory_cli")


def _memory_agent_runtime_module():
    return import_module("g3ku.agent.memory_agent_runtime")


def _memory_cfg():
    from g3ku.config.schema import MemoryToolsConfig

    payload = MemoryToolsConfig().model_dump(mode="python")
    payload["document"] = {
        "summary_max_chars": 100,
        "document_max_chars": 10000,
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


def _mount_real_memory_manager(monkeypatch, tmp_path: Path) -> None:
    memory_cli = _memory_cli_module()
    memory_agent_runtime = _memory_agent_runtime_module()
    monkeypatch.setattr(memory_cli.config_loader, "load_config", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(memory_cli, "load_tool_settings_from_manifest", lambda *args, **kwargs: _memory_cfg())
    monkeypatch.setattr(memory_cli.memory_agent_runtime, "MemoryManager", memory_agent_runtime.MemoryManager)


def test_memory_current_command_prints_snapshot(monkeypatch, tmp_path: Path) -> None:
    memory_cli = _memory_cli_module()

    class _StubManager:
        def snapshot_text(self) -> str:
            return "---\n2026/4/17-self：\n完成任务必须说明任务总耗时"

        def close(self) -> None:
            return None

    monkeypatch.setattr(memory_cli.config_loader, "load_config", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(memory_cli, "load_tool_settings_from_manifest", lambda *args, **kwargs: SimpleNamespace(enabled=True))
    monkeypatch.setattr(memory_cli.memory_agent_runtime, "MemoryManager", lambda workspace, cfg: _StubManager())

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    result = runner.invoke(app, ["current"])

    assert result.exit_code == 0
    assert any("完成任务必须说明任务总耗时" in message for message in console.messages)


def test_memory_flush_command_calls_manager_run_due_batch_once(monkeypatch, tmp_path: Path) -> None:
    memory_cli = _memory_cli_module()

    calls: dict[str, object] = {}

    class _StubManager:
        async def run_due_batch_once(self) -> dict[str, object]:
            calls["flush"] = True
            return {"ok": True, "status": "applied", "processed": 2}

        def close(self) -> None:
            return None

    monkeypatch.setattr(memory_cli.config_loader, "load_config", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(memory_cli, "load_tool_settings_from_manifest", lambda *args, **kwargs: SimpleNamespace(enabled=True))
    monkeypatch.setattr(memory_cli.memory_agent_runtime, "MemoryManager", lambda workspace, cfg: _StubManager())

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    result = runner.invoke(app, ["flush"])

    assert result.exit_code == 0
    assert calls == {"flush": True}
    assert any('"processed": 2' in message.lower() for message in console.messages)


def test_memory_doctor_reports_invalid_document_missing_refs_orphans_and_stuck_head(
    monkeypatch,
    tmp_path: Path,
) -> None:
    memory_cli = _memory_cli_module()
    _mount_real_memory_manager(monkeypatch, tmp_path)
    memory_dir = tmp_path / "memory"
    notes_dir = memory_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("broken entry with ref:note_missing\n", encoding="utf-8")
    (notes_dir / "orphan_note.md").write_text("orphan body\n", encoding="utf-8")
    (memory_dir / "queue.jsonl").write_text(
        json.dumps(
            {
                "op": "write",
                "decision_source": "user",
                "payload_text": "Prefer concise answers",
                "created_at": "2026-04-17T10:00:00+08:00",
                "request_id": "write_1",
                "status": "processing",
                "processing_started_at": "2026-04-17T10:00:00+08:00",
                "last_error_text": "",
                "last_error_at": "",
                "retry_after": "",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    result = runner.invoke(app, ["doctor", "--now-iso", "2026-04-17T10:10:00+08:00", "--stuck-after-seconds", "60"])

    joined = "\n".join(console.messages).lower()
    assert result.exit_code == 1
    assert "memory.md" in joined
    assert "invalid" in joined
    assert "missing note refs" in joined
    assert "orphan" in joined
    assert "stuck processing head" in joined


def test_memory_doctor_is_inspection_only_and_does_not_create_memory_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    memory_cli = _memory_cli_module()
    _mount_real_memory_manager(monkeypatch, tmp_path)
    memory_dir = tmp_path / "memory"
    memory_file = memory_dir / "MEMORY.md"
    queue_file = memory_dir / "queue.jsonl"
    ops_file = memory_dir / "ops.jsonl"
    notes_dir = memory_dir / "notes"

    assert not memory_dir.exists()

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert not memory_dir.exists()
    assert not memory_file.exists()
    assert not queue_file.exists()
    assert not ops_file.exists()
    assert not notes_dir.exists()


def test_memory_doctor_reports_malformed_queue_jsonl_and_exits_nonzero(
    monkeypatch,
    tmp_path: Path,
) -> None:
    memory_cli = _memory_cli_module()
    _mount_real_memory_manager(monkeypatch, tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("", encoding="utf-8")
    (memory_dir / "queue.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "op": "write",
                        "decision_source": "user",
                        "payload_text": "Prefer concise answers",
                        "created_at": "2026-04-17T10:00:00+08:00",
                        "request_id": "write_1",
                        "status": "pending",
                    },
                    ensure_ascii=False,
                ),
                '{"op":"write","decision_source":"user","payload_text":"broken"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    result = runner.invoke(app, ["doctor"])

    joined = "\n".join(console.messages).lower()
    assert result.exit_code == 1
    assert "queue parse errors" in joined
    assert "queue.jsonl" in joined
    assert "line 2" in joined


def test_memory_reconcile_notes_creates_missing_notes_and_deletes_orphans(
    monkeypatch,
    tmp_path: Path,
) -> None:
    memory_cli = _memory_cli_module()
    _mount_real_memory_manager(monkeypatch, tmp_path)
    memory_dir = tmp_path / "memory"
    notes_dir = memory_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text(
        "---\n2026/4/17-user锛歕nDeployment checklist, see ref:note_ops\n",
        encoding="utf-8",
    )
    (notes_dir / "orphan_note.md").write_text("orphan body\n", encoding="utf-8")

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    result = runner.invoke(app, ["reconcile-notes", "--delete-orphans"])

    joined = "\n".join(console.messages).lower()
    assert result.exit_code == 0
    assert (notes_dir / "note_ops.md").exists()
    assert not (notes_dir / "orphan_note.md").exists()
    assert "created 1 missing notes" in joined
    assert "deleted 1 orphan notes" in joined


def test_memory_import_legacy_is_dry_run_by_default_and_requires_apply_to_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    memory_cli = _memory_cli_module()
    _mount_real_memory_manager(monkeypatch, tmp_path)
    legacy_file = tmp_path / "legacy-memory.jsonl"
    legacy_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "date_text": "2026/4/17",
                        "source": "user",
                        "summary": "Prefer concise answers",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "date_text": "2026/4/17",
                        "source": "self",
                        "summary": "Deployment checklist, see ref:note_ops",
                        "note_ref": "note_ops",
                        "note_text": "1. Run smoke tests.\n2. Check worker logs.",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    dry_run = runner.invoke(app, ["import-legacy", str(legacy_file)])

    assert dry_run.exit_code == 0
    assert not (tmp_path / "memory" / "MEMORY.md").exists()
    assert not (tmp_path / "memory" / "queue.jsonl").exists()
    assert not (tmp_path / "memory" / "ops.jsonl").exists()
    assert not (tmp_path / "memory" / "notes" / "note_ops.md").exists()
    assert "dry-run" in "\n".join(console.messages).lower()

    console.messages.clear()
    applied = runner.invoke(app, ["import-legacy", str(legacy_file), "--apply"])
    joined = "\n".join(console.messages).lower()
    memory_text = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")

    assert applied.exit_code == 0
    assert "prefer concise answers" in memory_text.lower()
    assert "ref:note_ops" in memory_text.lower()
    assert (tmp_path / "memory" / "notes" / "note_ops.md").exists()
    assert "applied legacy import" in joined

    console.messages.clear()
    second_apply = runner.invoke(app, ["import-legacy", str(legacy_file), "--apply"])

    assert second_apply.exit_code == 1
    assert "non-empty" in "\n".join(console.messages).lower()


def test_memory_cleanup_legacy_is_dry_run_by_default_and_reports_targets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    memory_cli = _memory_cli_module()
    _mount_real_memory_manager(monkeypatch, tmp_path)
    memory_dir = tmp_path / "memory"
    context_store = memory_dir / "context_store"
    context_store.mkdir(parents=True, exist_ok=True)
    (memory_dir / "HISTORY.md").write_text("old history", encoding="utf-8")
    (memory_dir / "sync_journal.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    (context_store / "legacy.txt").write_text("legacy body", encoding="utf-8")

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    result = runner.invoke(app, ["cleanup-legacy"])
    joined = "\n".join(console.messages).lower()

    assert result.exit_code == 0
    assert "dry-run legacy cleanup" in joined
    assert "history.md" in joined
    assert "sync_journal.jsonl" in joined
    assert "context_store" in joined
    assert (memory_dir / "HISTORY.md").exists()
    assert (memory_dir / "sync_journal.jsonl").exists()
    assert (context_store / "legacy.txt").exists()


def test_memory_cleanup_legacy_refuses_apply_when_notebook_is_empty_and_import_may_still_be_needed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    memory_cli = _memory_cli_module()
    _mount_real_memory_manager(monkeypatch, tmp_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("", encoding="utf-8")
    (memory_dir / "HISTORY.md").write_text("old history", encoding="utf-8")

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    result = runner.invoke(app, ["cleanup-legacy", "--apply"])

    assert result.exit_code == 1
    assert "import-legacy" in "\n".join(console.messages).lower()
    assert (memory_dir / "HISTORY.md").exists()


def test_memory_cleanup_legacy_apply_deletes_legacy_files_after_migration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    memory_cli = _memory_cli_module()
    _mount_real_memory_manager(monkeypatch, tmp_path)
    memory_dir = tmp_path / "memory"
    context_store = memory_dir / "context_store"
    context_store.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("---\n2026/4/17-user：\nPrefer concise answers\n", encoding="utf-8")
    (memory_dir / "HISTORY.md").write_text("old history", encoding="utf-8")
    (memory_dir / "sync_journal.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    (context_store / "legacy.txt").write_text("legacy body", encoding="utf-8")

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    result = runner.invoke(app, ["cleanup-legacy", "--apply"])
    joined = "\n".join(console.messages).lower()

    assert result.exit_code == 0
    assert "applied legacy cleanup" in joined
    assert not (memory_dir / "HISTORY.md").exists()
    assert not (memory_dir / "sync_journal.jsonl").exists()
    assert not context_store.exists()


def test_memory_read_only_commands_support_two_arg_runtime_manager_stub(
    monkeypatch,
    tmp_path: Path,
) -> None:
    memory_cli = _memory_cli_module()
    calls: list[str] = []

    class _TwoArgRuntimeManager:
        def __init__(self, workspace, cfg):
            _ = (workspace, cfg)
            calls.append("runtime.init")

        def doctor_report(self, **kwargs):
            _ = kwargs
            calls.append("runtime.doctor")
            return {
                "ok": True,
                "status": "ok",
                "checks": [],
                "memory_document_valid": True,
                "memory_document_error": "",
                "missing_note_refs": [],
                "orphan_notes": [],
                "stuck_processing_head": None,
                "queue_parse_errors": [],
            }

        def import_legacy(self, legacy_path: Path, *, apply: bool):
            calls.append(f"runtime.import_legacy:{int(bool(apply))}")
            return {
                "ok": True,
                "legacy_path": str(legacy_path),
                "entry_count": 1,
                "note_ref_count": 0,
            }

        def close(self) -> None:
            calls.append("runtime.close")

    monkeypatch.setattr(memory_cli.config_loader, "load_config", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(memory_cli, "load_tool_settings_from_manifest", lambda *args, **kwargs: SimpleNamespace(enabled=True))
    monkeypatch.setattr(memory_cli.memory_agent_runtime, "MemoryManager", _TwoArgRuntimeManager)

    legacy_file = tmp_path / "legacy.jsonl"
    legacy_file.write_text(
        json.dumps(
            {
                "date_text": "2026/4/17",
                "source": "user",
                "summary": "Prefer concise answers",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    doctor_result = runner.invoke(app, ["doctor"])
    import_result = runner.invoke(app, ["import-legacy", str(legacy_file)])

    joined = "\n".join(console.messages).lower()
    assert doctor_result.exit_code == 0
    assert import_result.exit_code == 0
    assert "dry-run legacy import" in joined
    assert calls == [
        "runtime.init",
        "runtime.doctor",
        "runtime.close",
        "runtime.init",
        "runtime.import_legacy:0",
        "runtime.close",
    ]


def test_memory_cli_help_exposes_only_queued_markdown_commands() -> None:
    memory_cli = _memory_cli_module()
    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    top_help = runner.invoke(app, ["--help"])

    assert top_help.exit_code == 0

    top_output = top_help.output
    assert "current" in top_output
    assert "queue" in top_output
    assert "flush" in top_output
    assert "doctor" in top_output
    assert "reconcile-notes" in top_output
    assert "import-legacy" in top_output
    assert "cleanup-legacy" in top_output

    assert "stats" not in top_output
    assert "trace" not in top_output
    assert "explain" not in top_output
    assert "migrate-v2" not in top_output
    assert "reset-runtime" not in top_output
    assert "decay" not in top_output
    assert "pending" not in top_output


def test_memory_cli_routes_supported_commands_to_runtime_manager(
    monkeypatch,
    tmp_path: Path,
) -> None:
    memory_cli = _memory_cli_module()
    calls: list[str] = []

    class _RuntimeManager:
        def __init__(self, workspace, cfg):
            _ = (workspace, cfg)
            calls.append("runtime.init")

        def snapshot_text(self) -> str:
            calls.append("runtime.current")
            return "snapshot"

        async def list_queue(self, *, limit: int):
            calls.append(f"runtime.queue:{limit}")
            return [{"request_id": "q1"}]

        async def run_due_batch_once(self) -> dict[str, object]:
            calls.append("runtime.flush")
            return {"ok": True}

        def close(self) -> None:
            calls.append("runtime.close")

    monkeypatch.setattr(memory_cli.config_loader, "load_config", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(memory_cli, "load_tool_settings_from_manifest", lambda *args, **kwargs: SimpleNamespace(enabled=True))
    monkeypatch.setattr(memory_cli.memory_agent_runtime, "MemoryManager", _RuntimeManager)

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    current_result = runner.invoke(app, ["current"])
    queue_result = runner.invoke(app, ["queue", "--limit", "5"])
    flush_result = runner.invoke(app, ["flush"])

    assert current_result.exit_code == 0
    assert queue_result.exit_code == 0
    assert flush_result.exit_code == 0
    assert calls == [
        "runtime.init",
        "runtime.current",
        "runtime.close",
        "runtime.init",
        "runtime.queue:5",
        "runtime.close",
        "runtime.init",
        "runtime.flush",
        "runtime.close",
    ]
