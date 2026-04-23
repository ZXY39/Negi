from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

import g3ku.cli.commands as commands
import g3ku.web.launcher as launcher
import g3ku.web.main as web_main
from g3ku.deployment.runtime_startup import (
    BOOTSTRAP_PASSWORD_ENV,
    auto_unlock_from_env,
    ensure_persistent_workspace_dirs,
    seed_workspace_resources,
)
from main.service.task_terminal_callback import (
    TASK_TERMINAL_CALLBACK_TOKEN_ENV,
    TASK_TERMINAL_CALLBACK_URL_ENV,
)


class _SecurityStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._unlocked = False

    def is_unlocked(self) -> bool:
        return self._unlocked

    def status(self) -> dict[str, str]:
        return {"mode": "locked"}

    def unlock(self, *, password: str) -> dict[str, str]:
        self.calls.append(("password", password))
        self._unlocked = True
        return {"mode": "unlocked"}


def test_ensure_persistent_workspace_dirs_creates_runtime_and_resource_roots(tmp_path: Path) -> None:
    ensure_persistent_workspace_dirs(tmp_path)

    for relative in (".g3ku", "memory", "sessions", "temp", "externaltools", "skills", "tools"):
        assert (tmp_path / relative).is_dir()


def test_seed_workspace_resources_copies_missing_files_without_overwriting(tmp_path: Path) -> None:
    seed_root = tmp_path / "seed"
    (seed_root / "skills" / "demo").mkdir(parents=True)
    (seed_root / "tools" / "demo").mkdir(parents=True)
    (seed_root / "skills" / "demo" / "SKILL.md").write_text("seed-skill\n", encoding="utf-8")
    (seed_root / "tools" / "demo" / "resource.yaml").write_text("name: demo\n", encoding="utf-8")

    (tmp_path / "skills" / "demo").mkdir(parents=True)
    (tmp_path / "skills" / "demo" / "SKILL.md").write_text("local-skill\n", encoding="utf-8")

    copied = seed_workspace_resources(tmp_path, seed_root=seed_root)

    assert copied["tools"] == ["demo/resource.yaml"]
    assert (tmp_path / "tools" / "demo" / "resource.yaml").exists()
    assert (tmp_path / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "local-skill\n"


def test_auto_unlock_from_env_uses_password_when_master_key_is_absent(monkeypatch) -> None:
    security = _SecurityStub()
    monkeypatch.setenv(BOOTSTRAP_PASSWORD_ENV, "demo-password")

    assert auto_unlock_from_env(security_service=security) == "password"
    assert security.calls == [("password", "demo-password")]


def test_web_command_forwards_no_worker(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr("g3ku.shells.web.run_web_shell", lambda **kwargs: captured.update(kwargs))

    runner = CliRunner()
    result = runner.invoke(commands.app, ["web", "--no-worker"])

    assert result.exit_code == 0
    assert captured["with_worker"] is False


def test_prepare_web_server_start_uses_explicit_callback_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        TASK_TERMINAL_CALLBACK_URL_ENV,
        "http://web:18790/api/internal/task-terminal",
    )
    monkeypatch.setenv(TASK_TERMINAL_CALLBACK_TOKEN_ENV, "shared-token")
    monkeypatch.setattr(launcher, "_resolve_project_root", lambda: tmp_path)
    monkeypatch.setattr(launcher, "ensure_startup_config_ready", lambda: False)
    monkeypatch.setattr(launcher, "_ensure_frontend_ready", lambda: None)
    monkeypatch.setattr(launcher, "_resolve_web_bind", lambda host, port: ("0.0.0.0", 18790))
    monkeypatch.setattr(launcher, "_acquire_web_start_lock", lambda root, port: None)

    launcher.prepare_web_server_start(host=None, port=None, reload=False, with_worker=False)

    payload = json.loads((tmp_path / ".g3ku" / "internal-callback.json").read_text(encoding="utf-8"))
    assert payload["url"] == "http://web:18790/api/internal/task-terminal"
    assert payload["token"] == "shared-token"


def test_web_lifespan_attempts_env_auto_unlock_before_runtime_boot(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(web_main, "frontend_assets_available", lambda: True)
    monkeypatch.setattr(web_main, "ensure_frontend_vendor_assets", lambda: None)
    monkeypatch.setattr(web_main, "auto_unlock_from_env", lambda **_: calls.append("unlock") or "password")

    class _Security:
        def is_unlocked(self) -> bool:
            return False

    monkeypatch.setattr(web_main, "get_bootstrap_security_service", lambda: _Security())

    with TestClient(web_main.app):
        pass

    assert calls == ["unlock"]
