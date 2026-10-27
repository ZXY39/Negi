"""jail 管理器：会话卷、lease/idle、执行与文件桥（用假后端验生命周期）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import main  # noqa: F401  (production import order)
from g3ku.sandbox.backend_base import Enforcement
from g3ku.sandbox.bridge_files import export_file, import_file, list_area
from g3ku.sandbox.errors import SandboxDeniedError, SandboxUnavailableError
from g3ku.sandbox.manager import FSOP_JAIL_PATH, SandboxManager
from g3ku.sandbox.probe import FULL, MISSING

_REPO_FSOP = Path(__file__).resolve().parents[1] / "g3ku" / "sandbox" / "fsop.py"


class FakeBackend:
    """把 jail 内的 ``python3`` / 助手路径映射到本机，其余原样交给 subprocess。"""

    keeps_host_env = True
    # 假后端直接在宿主上跑，路径不重映射（与 Seatbelt 同形）——这样 fsop 的根就是
    # 真实的会话卷目录。
    maps_paths = False

    def __init__(self, name: str = "fake", enforcement: Enforcement | None = None):
        self.name = name
        self._enforcement = enforcement or Enforcement(
            fs_read=FULL, fs_write=FULL, network=FULL, process=FULL, resource=FULL
        )

    def probe(self):
        return {"state": "ready", "backend": self.name}

    def enforcement(self) -> Enforcement:
        return self._enforcement

    def create(self, spec) -> None:
        _ = spec

    def destroy(self, spec) -> None:
        _ = spec

    def build_argv(self, spec, argv, *, cwd="/workspace", env=None, read_paths=(), write_paths=()):
        _ = (spec, cwd, env, read_paths, write_paths)
        mapped = []
        for item in argv:
            if item == "python3":
                mapped.append(sys.executable)
            elif item == FSOP_JAIL_PATH:
                mapped.append(str(_REPO_FSOP))
            else:
                mapped.append(str(item))
        return mapped


def _manager(tmp_path: Path, *, backend=None, install=None) -> SandboxManager:
    return SandboxManager(
        base_dir=tmp_path / "sandboxes",
        backends=[backend or FakeBackend()],
        install_reading=install or {"state": "ready"},
    )


def test_third_party_needs_required_dimensions(tmp_path: Path) -> None:
    partial = FakeBackend(
        enforcement=Enforcement(fs_read=FULL, fs_write=FULL, network=MISSING, process=FULL, resource=FULL)
    )
    manager = _manager(tmp_path, backend=partial)
    assert manager.select(trust="third_party_group").backend is None
    assert manager.select(trust="owner").backend is partial


def test_ensure_creates_per_session_layout(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    handle = manager.ensure("ext:qq-official-1:abc", trust="third_party_group")
    assert handle.paths["workspace"]
    assert Path(handle.paths["workspace"]).is_dir()
    assert Path(handle.paths["inbox"]).is_dir()
    assert Path(handle.paths["outbox"]).is_dir()
    # 幂等：同会话拿到同一个 handle，不同会话不同卷
    assert manager.ensure("ext:qq-official-1:abc", trust="third_party_group") is handle
    other = manager.ensure("ext:qq-official-1:def", trust="third_party_group")
    assert other.spec.workspace_root != handle.spec.workspace_root


def test_ensure_without_backend_fails_closed(tmp_path: Path) -> None:
    manager = SandboxManager(base_dir=tmp_path / "sandboxes", backends=[], install_reading={"state": "not_installed"})
    with pytest.raises(SandboxUnavailableError):
        manager.ensure("ext:qq-official-1:abc", trust="third_party_group")


def test_run_and_fsop(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    handle = manager.ensure("web:ceo-1", trust="owner")

    result = manager.run(handle, [sys.executable, "-c", "print('hello')"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"

    target = Path(handle.paths["workspace"]) / "note.txt"
    written = manager.fsop(handle, {"op": "write", "path": str(target), "content": "hi"})
    assert written["ok"] is True
    read = manager.fsop(handle, {"op": "read", "path": str(target)})
    assert read["text"] == "hi"

    outside = tmp_path / "host.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(SandboxDeniedError):
        manager.fsop(handle, {"op": "read", "path": str(outside)})


def test_lease_and_idle_reap(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.ensure("web:ceo-1", trust="owner")
    manager.acquire("web:ceo-1")
    assert manager.reap_idle(max_idle_seconds=0) == []  # 有 lease 不动
    manager.release("web:ceo-1")
    assert manager.reap_idle(max_idle_seconds=0) == ["web:ceo-1"]
    assert manager.handle("web:ceo-1") is None
    # 回收只丢 handle，目录还在（卷保留）
    assert (tmp_path / "sandboxes").exists()


def test_forget_destroys_the_volume(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    handle = manager.ensure("web:ceo-1", trust="owner")
    workspace = Path(handle.paths["workspace"])
    assert workspace.is_dir()
    manager.forget("web:ceo-1")
    assert not workspace.exists()


def test_file_bridge_roundtrip_and_guards(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    handle = manager.ensure("web:ceo-1", trust="owner")

    source = tmp_path / "upload.txt"
    source.write_text("payload", encoding="utf-8")
    imported = import_file(handle, source, name="../../evil.txt")
    assert imported["name"] == "evil.txt"  # 路径分隔符被 safe_filename 吃掉
    assert (Path(handle.paths["inbox"]) / "evil.txt").read_text(encoding="utf-8") == "payload"

    listed = list_area(handle, area="inbox")
    assert [entry["name"] for entry in listed] == ["evil.txt"]

    (Path(handle.paths["outbox"]) / "result.txt").write_text("done", encoding="utf-8")
    name, payload = export_file(handle, "result.txt")
    assert name == "result.txt"
    assert payload == b"done"

    link = tmp_path / "link.txt"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not available on this platform")
    with pytest.raises(SandboxDeniedError):
        import_file(handle, link)
