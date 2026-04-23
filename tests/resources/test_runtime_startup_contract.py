from __future__ import annotations

from pathlib import Path

from g3ku.deployment.runtime_startup import (
    BOOTSTRAP_PASSWORD_ENV,
    auto_unlock_from_env,
    ensure_persistent_workspace_dirs,
    seed_workspace_resources,
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
