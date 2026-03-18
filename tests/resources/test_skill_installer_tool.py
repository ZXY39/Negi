from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import MethodType
from uuid import uuid4

import pytest

from g3ku.resources.loader import ResourceLoader
from g3ku.resources.registry import ResourceRegistry

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
        lambda self, *, source, method, tmp_dir: (source_repo, "mock"),
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
    assert "安装 skill" in resource_text
    assert "GitHub skill" in resource_text


def test_skill_installer_git_fallback_resets_temp_repo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_skill_installer_module()
    tool = module.SkillInstallerTool(workspace=tmp_path)
    source = module.GitHubSource(
        owner="openclaw",
        repo="skills",
        ref="main",
        path="skills/jakelin/ddg-web-search",
        url="https://github.com/openclaw/skills/tree/main/skills/jakelin/ddg-web-search",
    )
    repo_dir = tmp_path / "repo"
    clone_attempts = 0
    git_calls: list[list[str]] = []

    def fake_run_git(args: list[str]) -> None:
        nonlocal clone_attempts
        git_calls.append(list(args))
        if args[:2] != ["git", "clone"]:
            return
        clone_attempts += 1
        if clone_attempts == 1:
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "leftover.txt").write_text("stale", encoding="utf-8")
            raise module.InstallError("initial clone failed")
        assert not repo_dir.exists()
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
    assert result == repo_dir.resolve(strict=False)
    assert not (repo_dir / "leftover.txt").exists()
