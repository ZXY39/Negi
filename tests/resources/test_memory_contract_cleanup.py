from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner
import yaml

from g3ku.utils.helpers import sync_workspace_templates


class _ConsoleStub:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def print(self, *args, **kwargs) -> None:
        _ = kwargs
        self.messages.append(" ".join(str(item) for item in args))


def test_sync_workspace_templates_no_longer_creates_memory_history_placeholder(tmp_path: Path) -> None:
    created = sync_workspace_templates(tmp_path, silent=True)

    assert "memory/MEMORY.md" in created
    assert "memory/HISTORY.md" not in created
    assert (tmp_path / "memory" / "MEMORY.md").exists()
    assert not (tmp_path / "memory" / "HISTORY.md").exists()


def test_status_reports_current_memory_runtime_contract_without_legacy_store_fields(
    monkeypatch,
    tmp_path: Path,
) -> None:
    commands = import_module("g3ku.cli.commands")
    config_path = tmp_path / ".g3ku" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{}", encoding="utf-8")

    workspace = tmp_path
    memory_dir = workspace / "memory"
    notes_dir = memory_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("", encoding="utf-8")
    (memory_dir / "queue.jsonl").write_text("", encoding="utf-8")
    (memory_dir / "ops.jsonl").write_text("", encoding="utf-8")

    console = _ConsoleStub()
    monkeypatch.setattr(commands, "console", console)
    monkeypatch.setattr("g3ku.config.loader.get_config_path", lambda: config_path)
    monkeypatch.setattr(
        "g3ku.config.loader.load_config",
        lambda: SimpleNamespace(
            workspace_path=workspace,
            resolve_role_model_key=lambda role: f"{role}_model",
            agents=SimpleNamespace(defaults=SimpleNamespace(runtime="langgraph")),
            providers=SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        commands,
        "_load_memory_runtime_settings",
        lambda _config: SimpleNamespace(
            enabled=True,
            document=SimpleNamespace(memory_file="memory/MEMORY.md", notes_dir="memory/notes"),
            queue=SimpleNamespace(queue_file="memory/queue.jsonl", ops_file="memory/ops.jsonl"),
            checkpointer=SimpleNamespace(backend="sqlite", path="memory/checkpoints.sqlite3"),
        ),
    )
    monkeypatch.setattr("g3ku.providers.registry.PROVIDERS", [])

    runner = CliRunner()
    result = runner.invoke(commands.app, ["status"])
    output = "\n".join(console.messages)

    assert result.exit_code == 0
    assert "Memory Notebook:" in output
    assert "Memory Notes Dir:" in output
    assert "Memory Queue:" in output
    assert "Memory Ops Log:" in output
    assert "Memory Checkpointer:" in output
    assert "Memory Mode:" not in output
    assert "Memory Store(SQLite):" not in output
    assert "Memory Store(Qdrant):" not in output
    assert "Memory Pending File:" not in output
    assert "Memory Audit File:" not in output


def test_memory_runtime_manifest_drops_legacy_transition_fields() -> None:
    manifest = yaml.safe_load((Path("tools") / "memory_runtime" / "resource.yaml").read_text(encoding="utf-8")) or {}
    settings = dict(manifest.get("settings") or {})

    assert "mode" not in settings
    assert "backend" not in settings
    assert "bootstrap_mode" not in settings
    assert "compat" not in settings


def test_memory_tools_config_no_longer_exposes_legacy_transition_fields() -> None:
    from g3ku.config.schema import MemoryToolsConfig

    payload = MemoryToolsConfig().model_dump(mode="python")

    assert "mode" not in payload
    assert "backend" not in payload
    assert "bootstrap_mode" not in payload
    assert "compat" not in payload
    assert payload["store"]["qdrant_collection"] == "g3ku_memory"
