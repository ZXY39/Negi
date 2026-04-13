from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import g3ku.shells.memory_cli as memory_cli


class _ConsoleStub:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def print(self, *args, **kwargs) -> None:
        _ = kwargs
        self.messages.append(" ".join(str(item) for item in args))

    def print_json(self, payload: str) -> None:
        self.messages.append(str(payload))


def test_memory_reset_command_calls_manager_reset_runtime(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    class _StubManager:
        def reset_runtime(self, *, reason: str = "manual") -> dict[str, object]:
            calls["reason"] = reason
            return {"ok": True, "reset": True, "memory_dir": str(tmp_path / "memory")}

        def close(self) -> None:
            return None

    monkeypatch.setattr(memory_cli.config_loader, "load_config", lambda: SimpleNamespace(workspace_path=tmp_path))
    monkeypatch.setattr(memory_cli, "load_tool_settings_from_manifest", lambda *args, **kwargs: SimpleNamespace(enabled=True))
    monkeypatch.setattr(memory_cli.rag_memory, "MemoryManager", lambda workspace, cfg: _StubManager())

    console = _ConsoleStub()
    app = memory_cli.build_memory_app(console)
    runner = CliRunner()

    result = runner.invoke(app, ["reset-runtime", "--reason", "schema_scope_removal"])

    assert result.exit_code == 0
    assert calls == {"reason": "schema_scope_removal"}
    assert any('"reset": true' in message.lower() for message in console.messages)
