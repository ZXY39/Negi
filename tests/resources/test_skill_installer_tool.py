from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import MethodType
from uuid import uuid4

import pytest

from g3ku.resources.loader import ResourceLoader
from g3ku.resources.registry import ResourceRegistry
from g3ku.runtime.cancellation import ToolCancellationRequested, ToolCancellationToken

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_skill_installer_module():
    module_path = REPO_ROOT / "tools" / "skill-installer" / "main" / "tool.py"
    module_name = f"test_skill_installer_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubMainTaskService:
    def __init__(self) -> None:
        self.refresh_calls: list[dict[str, object]] = []

    def is_tool_action_allowed(
        self,
        *,
        actor_role: str,
        session_id: str,
        tool_id: str,
        action_id: str,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> bool:
        _ = actor_role, session_id, tool_id, action_id, task_id, node_id
        return True

    def refresh_resource_paths(self, paths, *, trigger: str = "path-change", session_id: str = "web:shared"):
        payload = {
            "paths": [str(Path(item)) for item in paths],
            "trigger": trigger,
            "session_id": session_id,
        }
        self.refresh_calls.append(payload)
        return {"ok": True, "session_id": session_id, "skills": 1, "tools": 1}


@pytest.mark.asyncio
async def test_skill_installer_imports_github_skill_and_generates_manifest(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    (workspace / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / "tools" / "skill-installer", workspace / "tools" / "skill-installer")

    source_repo = tmp_path / "source-repo"
    source_skill = source_repo / "skills" / "jakelin" / "ddg-web-search"
    (source_skill / "references").mkdir(parents=True, exist_ok=True)
    (source_skill / "SKILL.md").write_text(
        """---
name: ddg-web-search
description: Search the web with DuckDuckGo.
---
# ddg-web-search

Use DuckDuckGo for quick web lookups.
""",
        encoding="utf-8",
    )
    (source_skill / "references" / "guide.md").write_text("Example reference\n", encoding="utf-8")

    registry = ResourceRegistry(
        workspace,
        skills_dir=workspace / "skills",
        tools_dir=workspace / "tools",
    )
    descriptor = registry.discover().tools["skill-installer"]
    service = _StubMainTaskService()
    tool = ResourceLoader(workspace).load_tool(descriptor, services={"main_task_service": service})

    assert tool is not None
    handler = getattr(tool, "_handler", None)
    assert handler is not None

    handler._prepare_repo = MethodType(
        lambda self, *, source, method, tmp_dir, cancel_token=None: (source_repo, "mock"),
        handler,
    )

    payload = json.loads(
        await tool.execute(
            url="https://github.com/openclaw/skills/tree/main/skills/jakelin/ddg-web-search",
            __g3ku_runtime={"actor_role": "ceo", "session_key": "web:shared"},
        )
    )

    installed_root = workspace / "skills" / "ddg-web-search"
    assert payload["ok"] is True
    assert payload["tool"] == "skill-installer"
    assert payload["skill_id"] == "ddg-web-search"
    assert payload["installed_path"] == str(installed_root)
    assert payload["manifest_created"] is True
    assert payload["method"] == "mock"
    assert payload["catalog"]["ok"] is False

    manifest_text = (installed_root / "resource.yaml").read_text(encoding="utf-8")
    assert 'name: "ddg-web-search"' in manifest_text
    assert 'description: "Search the web with DuckDuckGo."' in manifest_text
    assert (installed_root / "references" / "guide.md").read_text(encoding="utf-8") == "Example reference\n"

    assert service.refresh_calls == [
        {
            "paths": [str(installed_root)],
            "trigger": "tool:skill-installer.install",
            "session_id": "web:shared",
        }
    ]

    skill_descriptor = ResourceRegistry(
        workspace,
        skills_dir=workspace / "skills",
        tools_dir=workspace / "tools",
    ).discover().skills["ddg-web-search"]
    assert skill_descriptor.available is True
    assert skill_descriptor.main_path == installed_root / "SKILL.md"


def test_skill_creator_routes_github_skill_installs_to_skill_installer():
    skill_text = (REPO_ROOT / "skills" / "skill-creator" / "SKILL.md").read_text(encoding="utf-8")
    resource_text = (REPO_ROOT / "skills" / "skill-creator" / "resource.yaml").read_text(encoding="utf-8")

    assert "优先直接调用 `skill-installer` 工具" in skill_text
    assert "优先转交给 skill-installer" in skill_text
    assert "GitHub repo/path" in skill_text
    assert "skill-installer" in resource_text


def test_skill_installer_git_fallback_uses_separate_temp_repo_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_skill_installer_module()
    tool = module.SkillInstallerTool(workspace=tmp_path)
    source = module.GitHubSource(
        owner="openclaw",
        repo="skills",
        ref="main",
        path="skills/jakelin/ddg-web-search",
        url="https://github.com/openclaw/skills/tree/main/skills/jakelin/ddg-web-search",
    )
    clone_attempts = 0
    git_calls: list[list[str]] = []
    clone_targets: list[Path] = []

    def fake_run_git(args: list[str], *, timeout: int, cancel_token=None) -> None:
        nonlocal clone_attempts
        git_calls.append(list(args))
        if args[:2] != ["git", "clone"]:
            return
        clone_attempts += 1
        repo_dir = Path(args[-1])
        clone_targets.append(repo_dir)
        if clone_attempts == 1:
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "leftover.txt").write_text("stale", encoding="utf-8")
            raise module.InstallError("initial clone failed")
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / ".git").write_text("ok", encoding="utf-8")

    monkeypatch.setattr(module, "_run_git", fake_run_git)
    monkeypatch.setattr(module.shutil, "which", lambda name: "git" if name == "git" else None)

    result = tool._git_sparse_checkout(source=source, tmp_dir=str(tmp_path))

    clone_calls = [call for call in git_calls if call[:2] == ["git", "clone"]]
    assert clone_attempts == 2
    assert len(clone_calls) == 2
    assert "--branch" in clone_calls[0]
    assert "--branch" not in clone_calls[1]
    assert len(set(clone_targets)) == 2
    assert clone_targets[0].name == "repo-branch"
    assert clone_targets[1].name == "repo-fallback"
    assert result == clone_targets[1].resolve(strict=False)
    assert (clone_targets[0] / "leftover.txt").exists()


@pytest.mark.asyncio
async def test_skill_installer_execute_uses_ignore_cleanup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_skill_installer_module()
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    source_repo = tmp_path / "source-repo"
    source_skill = source_repo / "skills" / "jakelin" / "ddg-web-search"
    source_skill.mkdir(parents=True, exist_ok=True)
    (source_skill / "SKILL.md").write_text("# ddg-web-search\n\nDuckDuckGo helper.\n", encoding="utf-8")

    captured: dict[str, object] = {}

    class _FakeTempDir:
        def __init__(self, *args, **kwargs) -> None:
            captured["tempdir_args"] = args
            captured["tempdir_kwargs"] = dict(kwargs)
            self._path = tmp_path / "fake-tempdir"
            self._path.mkdir(parents=True, exist_ok=True)

        def __enter__(self):
            return str(self._path)

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", _FakeTempDir)

    tool = module.SkillInstallerTool(workspace=workspace)
    monkeypatch.setattr(
        tool,
        "_prepare_repo",
        lambda *, source, method, tmp_dir, cancel_token=None: (source_repo, "mock"),
    )

    payload = json.loads(
        await tool.execute(
            url="https://github.com/openclaw/skills/tree/main/skills/jakelin/ddg-web-search",
            __g3ku_runtime={"actor_role": "ceo", "session_key": "web:shared"},
        )
    )

    assert payload["ok"] is True
    assert captured["tempdir_kwargs"]["ignore_cleanup_errors"] is True


def test_skill_installer_auto_prefers_git_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_skill_installer_module()
    settings = module.SkillInstallerToolSettings(auto_prefer="git")
    tool = module.SkillInstallerTool(workspace=tmp_path, settings=settings)
    source = module.GitHubSource(
        owner="openclaw",
        repo="skills",
        ref="main",
        path="skills/jakelin/ddg-web-search",
        url="https://github.com/openclaw/skills/tree/main/skills/jakelin/ddg-web-search",
    )
    calls: list[str] = []

    monkeypatch.setattr(
        tool,
        "_git_sparse_checkout",
        lambda *, source, tmp_dir, cancel_token=None: calls.append("git") or (tmp_path / "repo-git"),
    )
    monkeypatch.setattr(
        tool,
        "_download_repo_zip",
        lambda *, source, tmp_dir, cancel_token=None: calls.append("download") or (tmp_path / "repo-download"),
    )

    repo_root, method_used = tool._prepare_repo(source=source, method="auto", tmp_dir=str(tmp_path))

    assert repo_root == tmp_path / "repo-git"
    assert method_used == "git"
    assert calls == ["git"]


def test_skill_installer_run_git_uses_timeout_and_noninteractive_env(monkeypatch: pytest.MonkeyPatch):
    module = _load_skill_installer_module()
    captured: dict[str, object] = {}

    class _FakeProcess:
        def __init__(self, args, **kwargs) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs
            self.returncode = 0

        def poll(self):
            return 0

        def communicate(self):
            return "", ""

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            return None

    monkeypatch.setattr(module.subprocess, "Popen", _FakeProcess)

    module._run_git(["git", "clone", "demo"], timeout=77)

    assert captured["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["kwargs"]["env"]["GCM_INTERACTIVE"] == "never"


def test_skill_installer_run_git_terminates_process_when_cancelled(monkeypatch: pytest.MonkeyPatch):
    module = _load_skill_installer_module()
    captured = {"terminate": 0, "kill": 0}
    token = ToolCancellationToken(session_key="web:shared")
    token.cancel(reason="用户已请求暂停，正在安全停止...")

    class _FakeProcess:
        def __init__(self, args, **kwargs) -> None:
            _ = args, kwargs
            self.returncode = None

        def poll(self):
            return self.returncode

        def communicate(self):
            return "", ""

        def terminate(self):
            captured["terminate"] += 1
            self.returncode = -15

        def wait(self, timeout=None):
            _ = timeout
            return self.returncode

        def kill(self):
            captured["kill"] += 1
            self.returncode = -9

    monkeypatch.setattr(module.subprocess, "Popen", _FakeProcess)

    with pytest.raises(ToolCancellationRequested):
        module._run_git(["git", "clone", "demo"], timeout=30, cancel_token=token)

    assert captured["terminate"] >= 1
    assert captured["kill"] == 0


@pytest.mark.asyncio
async def test_skill_installer_execute_offloads_blocking_work_to_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_skill_installer_module()
    tool = module.SkillInstallerTool(workspace=tmp_path)
    captured: dict[str, object] = {}

    def fake_execute_blocking(loop, runtime, url, repo, path, ref, dest, name, method):
        captured["loop"] = loop
        captured["runtime"] = runtime
        captured["url"] = url
        return json.dumps({"ok": True, "tool": "skill-installer"})

    async def fake_to_thread(func, *args):
        captured["func"] = func
        captured["args"] = args
        return func(*args)

    monkeypatch.setattr(tool, "_execute_blocking", fake_execute_blocking)
    monkeypatch.setattr(module.asyncio, "to_thread", fake_to_thread)

    payload = json.loads(
        await tool.execute(
            url="https://github.com/openclaw/skills/tree/main/skills/jakelin/ddg-web-search",
            __g3ku_runtime={"actor_role": "ceo", "session_key": "web:shared"},
        )
    )

    assert payload["ok"] is True
    assert captured["func"] == fake_execute_blocking
    assert captured["runtime"] == {"actor_role": "ceo", "session_key": "web:shared"}
