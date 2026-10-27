"""会话卷文件管理端点：列表、导入、导出、重置。

用假后端（``maps_paths=False``）+ 真 ``bridge_files``/``fsop``：文件语义是真的，
只有内核隔离被跳过。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

import main  # noqa: F401  (production import order)
from g3ku.runtime.api import sandbox as sandbox_api
from g3ku.sandbox.backend_base import Enforcement
from g3ku.sandbox.manager import SandboxManager
from g3ku.sandbox.probe import FULL


class HostMappedBackend:
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


@pytest.fixture()
def sandbox_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manager = SandboxManager(
        base_dir=tmp_path / "sandboxes",
        backends=[HostMappedBackend()],
        install_reading={"state": "ready"},
    )
    staging = tmp_path / "incoming"
    staging.mkdir()

    monkeypatch.setattr(sandbox_api, "_session_exists", lambda key: True)
    monkeypatch.setattr(sandbox_api, "install_state", lambda: {"state": "ready"})
    monkeypatch.setattr(sandbox_api, "_sandbox_manager", lambda: manager)
    monkeypatch.setattr(sandbox_api, "_staging_dir", lambda: staging)
    return manager, tmp_path


def _upload(name: str, payload: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(payload), filename=name)


@pytest.mark.asyncio
async def test_files_are_not_listed_when_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_api, "_session_exists", lambda key: True)
    monkeypatch.setattr(sandbox_api, "install_state", lambda: {"state": "not_installed"})
    with pytest.raises(Exception) as excinfo:
        await sandbox_api.get_session_sandbox_files("web:ceo-1", area="inbox")
    assert getattr(excinfo.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_import_export_roundtrip(sandbox_env) -> None:
    manager, _tmp = sandbox_env
    handle = manager.ensure("web:ceo-1", trust="owner")

    imported = await sandbox_api.post_session_sandbox_import(
        "web:ceo-1", file=_upload("../../evil.txt", b"payload")
    )
    assert imported["ok"] is True
    # 单段文件名：路径分隔符被吃掉
    name = imported["item"]["name"]
    assert "/" not in name and "\\" not in name
    assert (Path(handle.paths["inbox"]) / name).read_bytes() == b"payload"

    listed = await sandbox_api.get_session_sandbox_files("web:ceo-1", area="inbox")
    assert [entry["name"] for entry in listed["item"]["entries"]] == [name]

    # 导出只从指定区取；workspace 里没有这个文件 → 404
    with pytest.raises(Exception) as missing:
        await sandbox_api.get_session_sandbox_export("web:ceo-1", name=name, area="workspace")
    assert getattr(missing.value, "status_code", None) == 404

    (Path(handle.paths["outbox"]) / "result.txt").write_text("done", encoding="utf-8")
    response = await sandbox_api.get_session_sandbox_export("web:ceo-1", name="result.txt", area="outbox")
    assert response.body == b"done"
    assert "result.txt" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_export_rejects_traversal(sandbox_env) -> None:
    manager, _tmp = sandbox_env
    handle = manager.ensure("web:ceo-1", trust="owner")
    (Path(handle.paths["workspace"]).parent / "secret.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        await sandbox_api.get_session_sandbox_export("web:ceo-1", name="../secret.txt", area="outbox")
    # safe_filename 会把路径压成单段，所以只会变成"文件不存在"
    assert getattr(excinfo.value, "status_code", None) in {403, 404}


@pytest.mark.asyncio
async def test_reset_requires_confirmation_and_clears_volume(sandbox_env) -> None:
    manager, _tmp = sandbox_env
    handle = manager.ensure("web:ceo-1", trust="owner")
    target = Path(handle.paths["workspace"]) / "note.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(Exception) as missing:
        await sandbox_api.post_session_sandbox_reset("web:ceo-1", {"confirm": False})
    assert getattr(missing.value, "status_code", None) == 400
    assert target.exists()

    result = await sandbox_api.post_session_sandbox_reset("web:ceo-1", {"confirm": True})
    assert result["ok"] is True
    assert not target.exists()
    assert Path(result["paths"]["workspace"]).is_dir()
