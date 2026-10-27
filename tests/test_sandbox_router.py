"""数据面工具 → jail 代理：路径映射、edit 三种模式、文件操作与拒识。

用 ``maps_paths=False`` 的假后端 + 真 ``fsop`` 跑：文件语义是真的（真的读写、真的
按根校验），只有内核隔离由假后端跳过——那部分靠 profile 断言与真机 e2e。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import main  # noqa: F401  (production import order)
from g3ku.agent.tools.base import Tool
from g3ku.sandbox.backend_base import Enforcement, ExecResult
from g3ku.sandbox.manager import SandboxManager
from g3ku.sandbox.probe import FULL
from g3ku.sandbox.router import JAILED_TOOL_NAMES, JailToolProxy, build_jailed_tools

_REPO_FSOP = Path(__file__).resolve().parents[1] / "g3ku" / "sandbox" / "fsop.py"


class HostMappedBackend:
    """Seatbelt 式的路径映射：没有 mount namespace，路径就是宿主路径。"""

    name = "fake"
    maps_paths = False
    keeps_host_env = True

    def enforcement(self) -> Enforcement:
        return Enforcement(fs_read=FULL, fs_write=FULL, network=FULL, process=FULL, resource=FULL)

    def probe(self):
        return {"state": "ready"}

    def create(self, spec) -> None:
        _ = spec

    def destroy(self, spec) -> None:
        _ = spec

    def build_argv(self, spec, argv, *, cwd="/workspace", env=None, read_paths=(), write_paths=()):
        _ = (spec, cwd, env, read_paths, write_paths)
        return [sys.executable if item == "python3" else str(item) for item in argv]


class _NamedTool(Tool):
    def __init__(self, name: str):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} description"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def execute(self, **kwargs):
        return "host"


@pytest.fixture()
def sandbox(tmp_path: Path):
    manager = SandboxManager(
        base_dir=tmp_path / "sandboxes",
        backends=[HostMappedBackend()],
        install_reading={"state": "ready"},
    )
    handle = manager.ensure("web:ceo-1", trust="owner")
    workspace = Path(handle.paths["workspace"])
    return manager, handle, workspace


def _run(proxy: JailToolProxy, **kwargs):
    return asyncio.run(proxy.execute(**kwargs))


def test_contract_is_forwarded_byte_for_byte(tmp_path: Path) -> None:
    base = _NamedTool("filesystem_write")
    manager = SandboxManager(base_dir=tmp_path, backends=[HostMappedBackend()], install_reading={"state": "ready"})
    handle = manager.ensure("web:ceo-1", trust="owner")
    proxy = JailToolProxy(base_tool=base, manager=manager, handle=handle)
    assert proxy.name == base.name
    assert proxy.description == base.description
    assert proxy.parameters == base.parameters
    assert proxy.to_model_schema() == base.to_model_schema()


def test_only_data_plane_tools_are_proxied(tmp_path: Path) -> None:
    manager = SandboxManager(base_dir=tmp_path, backends=[HostMappedBackend()], install_reading={"state": "ready"})
    handle = manager.ensure("web:ceo-1", trust="owner")
    base_tools = [
        _NamedTool("exec"),
        _NamedTool("filesystem_write"),
        _NamedTool("memory_update"),
        _NamedTool("create_async_task"),
        _NamedTool("filesystem_propose_patch"),
        _NamedTool("process_kill"),
    ]
    proxies = build_jailed_tools(base_tools, manager=manager, handle=handle)
    assert set(proxies) == {"exec", "filesystem_write"}
    assert set(proxies).issubset(JAILED_TOOL_NAMES)


def test_write_edit_delete_roundtrip(sandbox) -> None:
    manager, handle, workspace = sandbox
    tool = JailToolProxy(
        base_tool=_NamedTool("filesystem_write"), manager=manager, handle=handle
    )

    written = _run(tool, path="note.txt", content="alpha\nbeta\ngamma\n")
    assert written.startswith("Successfully wrote")
    assert "/workspace/note.txt" in written
    assert (workspace / "note.txt").read_text(encoding="utf-8").startswith("alpha")

    edit = JailToolProxy(base_tool=_NamedTool("filesystem_edit"), manager=manager, handle=handle)
    result = _run(edit, path="/workspace/note.txt", target={"by": "exact_text", "text": "beta"}, new_text="BETA")
    assert result.startswith("Successfully edited")
    assert "BETA" in (workspace / "note.txt").read_text(encoding="utf-8")

    ranged = _run(
        edit,
        path="/workspace/note.txt",
        mode="line_range",
        start_line=1,
        end_line=1,
        replacement="ALPHA\n",
    )
    assert ranged.startswith("Successfully edited")
    assert (workspace / "note.txt").read_text(encoding="utf-8").startswith("ALPHA")

    delete = JailToolProxy(base_tool=_NamedTool("filesystem_delete"), manager=manager, handle=handle)
    removed = _run(delete, paths=["/workspace/note.txt"])
    assert removed.startswith("Deleted 1 path")
    assert not (workspace / "note.txt").exists()


def test_copy_and_move(sandbox) -> None:
    manager, handle, workspace = sandbox
    (workspace / "src.txt").write_text("payload", encoding="utf-8")
    copy = JailToolProxy(base_tool=_NamedTool("filesystem_copy"), manager=manager, handle=handle)
    assert _run(copy, operations=[{"source": "/workspace/src.txt", "destination": "/workspace/copy.txt"}]).startswith(
        "Copied 1"
    )
    move = JailToolProxy(base_tool=_NamedTool("filesystem_move"), manager=manager, handle=handle)
    assert _run(move, operations=[{"source": "/workspace/copy.txt", "destination": "/workspace/moved.txt"}]).startswith(
        "Moved 1"
    )
    assert (workspace / "moved.txt").read_text(encoding="utf-8") == "payload"
    assert not (workspace / "copy.txt").exists()


def test_host_paths_are_refused(sandbox) -> None:
    manager, handle, workspace = sandbox
    tool = JailToolProxy(base_tool=_NamedTool("filesystem_write"), manager=manager, handle=handle)
    outside = "C:\\Windows\\system.ini" if sys.platform == "win32" else "/etc/passwd"
    result = _run(tool, path=outside, content="x")
    assert result.startswith("Error:")
    assert "not reachable inside the sandbox" in result
    assert not (workspace / "system.ini").exists()


def test_content_open_search_describe(sandbox) -> None:
    manager, handle, workspace = sandbox
    (workspace / "doc.md").write_text("line one\nalpha beta\ngamma\n", encoding="utf-8")

    opened = JailToolProxy(base_tool=_NamedTool("content_open"), manager=manager, handle=handle)
    payload = json.loads(_run(opened, path="/workspace/doc.md", start_line=2, end_line=2))
    assert payload["text"] == "alpha beta"
    assert payload["path"] == "/workspace/doc.md"

    ref_only = _run(opened, ref="artifact:abc")
    assert "not available inside the sandbox" in ref_only

    searched = JailToolProxy(base_tool=_NamedTool("content_search"), manager=manager, handle=handle)
    matches = json.loads(_run(searched, path="/workspace/doc.md", query="alpha"))
    assert matches["matches"] == [{"line": 2, "text": "alpha beta"}]

    described = JailToolProxy(base_tool=_NamedTool("content_describe"), manager=manager, handle=handle)
    meta = json.loads(_run(described, path="/workspace/doc.md"))
    assert meta["kind"] == "file"
    assert "line one" in meta["preview"]


def test_exec_payload_and_background_refusal(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, handle, workspace = sandbox
    tool = JailToolProxy(base_tool=_NamedTool("exec"), manager=manager, handle=handle)

    def fake_run(handle, argv, *, cwd, timeout, env=None):  # noqa: ANN001
        assert argv[:2] == ["sh", "-lc"]
        assert cwd == str(workspace)
        return ExecResult(exit_code=0, stdout="hello\n", stderr="")

    monkeypatch.setattr(manager, "run", fake_run)
    payload = json.loads(_run(tool, command="echo hello"))
    assert payload["status"] == "success"
    assert payload["head_preview"] == "hello\n"

    refused = _run(tool, command="sleep 100", background=True)
    assert "background processes are not supported" in refused


def test_unavailable_overrides_never_execute(tmp_path: Path) -> None:
    from g3ku.sandbox.router import SandboxUnavailableProxy, build_jail_overrides

    base = [_NamedTool("exec"), _NamedTool("memory_update")]
    proxies = build_jail_overrides(base, manager=None, handle=None, reason="backend is down")
    assert set(proxies) == {"exec"}
    proxy = proxies["exec"]
    assert isinstance(proxy, SandboxUnavailableProxy)
    result = asyncio.run(proxy.execute(command="echo hi"))
    assert "sandbox unavailable" in result
    # 契约照旧转发，模型看到的 schema 不变
    assert proxy.parameters == base[0].parameters


def test_stat_reports_directory_entries(sandbox) -> None:
    manager, handle, workspace = sandbox
    (workspace / "a.txt").write_text("abc", encoding="utf-8")
    tool = JailToolProxy(base_tool=_NamedTool("filesystem_stat"), manager=manager, handle=handle)
    payload = json.loads(_run(tool, paths=["/workspace/a.txt", "/workspace"]))
    assert payload["items"][0]["exists"] is True
    assert payload["items"][0]["size"] == 3
    assert any(entry["name"] == "a.txt" for entry in payload["items"][1]["entries"])
