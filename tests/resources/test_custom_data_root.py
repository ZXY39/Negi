"""自定义数据目录（data root）的解析与换锚契约。

判据核心：未配置时解析结果必须与历史路径（进程 cwd）逐字节相同，
否则存量安装会被静默搬家。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from g3ku.deployment import data_root as dr


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv(dr.DATA_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    dr.reset_cache()
    yield monkeypatch, tmp_path
    dr.reset_cache()


def test_default_root_is_cwd(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    assert dr.data_root() == Path(os.path.normpath(str(tmp_path)))
    assert dr.data_root_source() == dr.SOURCE_DEFAULT
    assert dr.describe_data_root()["is_default"] is True


def test_data_g3ku_path_matches_legacy_cwd_layout(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    legacy = tmp_path / ".g3ku" / "main-runtime" / "runtime.sqlite3"
    assert dr.data_g3ku_path("main-runtime", "runtime.sqlite3") == legacy


def test_env_wins_over_pointer(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    pointer_target = tmp_path / "pointer-dir"
    env_target = tmp_path / "env-dir"
    dr.write_data_root_pointer(pointer_target)
    assert dr.data_root() == pointer_target
    monkeypatch.setenv(dr.DATA_DIR_ENV, str(env_target))
    dr.reset_cache()
    assert dr.data_root() == env_target
    assert dr.data_root_source() == dr.SOURCE_ENV


def test_pointer_persists_as_json_and_is_reeadable(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    target = tmp_path / "D-data"
    info = dr.write_data_root_pointer(target)
    assert info["source"] == dr.SOURCE_POINTER
    payload = json.loads(dr.pointer_path().read_text(encoding="utf-8"))
    assert payload["data_dir"] == str(target)
    dr.reset_cache()
    assert dr.data_root() == target


def test_resolve_data_path_keeps_absolute_and_roots_relative(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    dr.write_data_root_pointer(tmp_path / "moved")
    relative = dr.resolve_data_path(".g3ku/main-runtime/runtime.sqlite3")
    assert relative == tmp_path / "moved" / ".g3ku" / "main-runtime" / "runtime.sqlite3"
    absolute = dr.resolve_data_path(str(tmp_path / "elsewhere" / "x.sqlite3"))
    assert absolute == tmp_path / "elsewhere" / "x.sqlite3"


def test_resolve_data_path_falls_back_to_default_when_config_silent(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    resolved = dr.resolve_data_path(
        None,
        default=".g3ku/main-runtime/governance.sqlite3",
    )
    assert resolved == tmp_path / ".g3ku" / "main-runtime" / "governance.sqlite3"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "data_root_empty"),
        ("relative/dir", "data_root_relative"),
    ],
)
def test_validation_rejects(clean_env, raw, code) -> None:
    monkeypatch, tmp_path = clean_env
    with pytest.raises(dr.DataRootError) as exc:
        dr.validate_data_root(raw)
    assert exc.value.code == code


def test_validation_rejects_install_ancestor(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    nested = tmp_path / "install" / "tree"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    with pytest.raises(dr.DataRootError) as exc:
        dr.validate_data_root(tmp_path)
    assert exc.value.code == "data_root_contains_install"


def test_validation_rejects_inside_config_dir_and_files(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    with pytest.raises(dr.DataRootError) as exc:
        dr.validate_data_root(tmp_path / ".g3ku" / "data")
    assert exc.value.code == "data_root_inside_config"

    blocked = tmp_path / "plain-file.txt"
    blocked.write_text("x", encoding="utf-8")
    with pytest.raises(dr.DataRootError) as exc:
        dr.validate_data_root(blocked)
    assert exc.value.code == "data_root_is_file"


def test_validation_rejects_unwritable_target(clean_env, monkeypatch) -> None:
    monkeypatch, tmp_path = clean_env
    target = tmp_path / "ro"

    def _deny(*args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(Path, "mkdir", _deny)
    with pytest.raises(dr.DataRootError) as exc:
        dr.validate_data_root(target)
    assert exc.value.code == "data_root_unwritable"


def test_validation_allows_current_root_as_noop(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    assert dr.validate_data_root(tmp_path) == tmp_path


def test_broken_pointer_falls_back_to_default(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    dr.pointer_path().parent.mkdir(parents=True, exist_ok=True)
    dr.pointer_path().write_text("{not json", encoding="utf-8")
    dr.reset_cache()
    assert dr.data_root() == Path(os.path.normpath(str(tmp_path)))
    assert dr.data_root_source() == dr.SOURCE_DEFAULT


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called: {kwargs!r}")


def _make_service(**kwargs):
    from main.service.runtime_service import MainRuntimeService

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        execution_mode="web",
        **kwargs,
    )
    service._assert_worker_available = lambda: None
    return service


def test_task_runtime_storage_follows_data_root(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    data_dir = tmp_path / "D-data"
    dr.write_data_root_pointer(data_dir)

    service = _make_service()

    assert service.store.path == data_dir / ".g3ku" / "main-runtime" / "runtime.sqlite3"
    assert Path(service.file_store.base_dir) == data_dir / ".g3ku" / "main-runtime" / "tasks"
    assert Path(service._deliverables_dir) == data_dir / ".g3ku" / "main-runtime" / "deliverables"
    assert service._task_temp_root(create=False) == data_dir / "temp" / "tasks"
    assert service._task_temp_dir("task:abc", create=True).parent == data_dir / "temp" / "tasks"


def test_task_runtime_storage_stays_on_cwd_without_pointer(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(dr.DATA_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    dr.reset_cache()

    service = _make_service()

    assert service.store.path == tmp_path / ".g3ku" / "main-runtime" / "runtime.sqlite3"
    assert service._task_temp_root(create=False) == tmp_path / "temp" / "tasks"
    assert (tmp_path / ".g3ku" / "main-runtime").is_dir()


def test_explicit_absolute_store_path_wins_over_data_root(clean_env) -> None:
    monkeypatch, tmp_path = clean_env
    dr.write_data_root_pointer(tmp_path / "D-data")
    override = tmp_path / "picked" / "runtime.sqlite3"

    service = _make_service(store_path=override)

    assert service.store.path == override


def test_disk_waterline_probes_data_root_not_install_root(clean_env, monkeypatch) -> None:
    monkeypatch, tmp_path = clean_env
    data_dir = tmp_path / "D-data"
    dr.write_data_root_pointer(data_dir)
    captured: list[list[str]] = []

    def _fake_snapshot(paths, **kwargs):
        captured.append([str(p) for p in paths])
        return (10 * 1024**3, 100 * 1024**3)

    monkeypatch.setattr("main.service.runtime_service.disk_waterline_snapshot", _fake_snapshot)
    service = _make_service()

    assert service._disk_waterline() is not None
    assert captured == [[str(data_dir)]]


def test_web_ceo_storage_follows_data_root(clean_env) -> None:
    from g3ku.runtime import web_ceo_sessions as ceo

    monkeypatch, tmp_path = clean_env
    data_dir = tmp_path / "D-data"
    dr.write_data_root_pointer(data_dir)

    assert ceo.actual_request_dir_for_session("web:ceo-x").parent == data_dir / ".g3ku" / "web-ceo-requests"
    assert ceo.upload_dir_for_session("web:ceo-x").parent == data_dir / ".g3ku" / "web-ceo-uploads"
    assert str(data_dir) in str(ceo.turn_boundary_dir_for_session("web:ceo-x"))
    assert str(data_dir) in str(ceo.completed_continuity_snapshot_path_for_session("web:ceo-x"))
    # 会话工作区（agent 沙箱）不随数据根迁移；交付默认落点是数据根下的 `output/`。
    assert ceo.workspace_path() == Path(os.path.normpath(str(tmp_path)))


def _bootstrap_client(monkeypatch, mode: str):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import main.api.bootstrap_rest as bootstrap_rest

    calls: dict[str, object] = {}

    class _Security:
        def status(self):
            return {"mode": mode, "legacy_detected": False, "auto_unlock": False}

        def is_unlocked(self):
            return mode == "unlocked"

        def setup_initial_realm(self, **kwargs):
            calls["setup_initial_realm"] = kwargs
            return {"mode": "locked"}

        def lock(self):
            calls["lock"] = True

    async def _no_runtime_start():
        return None

    monkeypatch.setattr(bootstrap_rest, "_service", lambda: _Security())
    monkeypatch.setattr(bootstrap_rest, "_start_runtime_after_unlock", _no_runtime_start)
    app = FastAPI()
    app.include_router(bootstrap_rest.router)
    return TestClient(app), calls


def test_setup_endpoint_reports_data_root_in_status(clean_env, monkeypatch) -> None:
    monkeypatch, tmp_path = clean_env
    client, _calls = _bootstrap_client(monkeypatch, "setup")

    payload = client.get("/bootstrap/status").json()["item"]

    assert payload["data_root"]["source"] == dr.SOURCE_DEFAULT
    assert payload["data_root"]["default_root"] == str(Path(os.path.normpath(str(tmp_path))))


def test_setup_endpoint_accepts_data_dir_before_realm_setup(clean_env, monkeypatch) -> None:
    monkeypatch, tmp_path = clean_env
    client, calls = _bootstrap_client(monkeypatch, "setup")
    target = tmp_path / "D-data"

    response = client.post(
        "/bootstrap/setup",
        json={"password": "a", "password_confirm": "a", "data_dir": str(target)},
    )

    assert response.status_code == 200, response.text
    assert dr.pointer_value() == str(target)
    assert dr.data_root() == target
    assert "setup_initial_realm" in calls


def test_setup_endpoint_rejects_relative_data_dir(clean_env, monkeypatch) -> None:
    monkeypatch, tmp_path = clean_env
    client, calls = _bootstrap_client(monkeypatch, "setup")

    response = client.post(
        "/bootstrap/setup",
        json={"password": "a", "password_confirm": "a", "data_dir": "D:relative"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "data_root_relative"
    assert "setup_initial_realm" not in calls
    assert dr.pointer_value() == ""


def test_setup_endpoint_refuses_data_dir_after_setup(clean_env, monkeypatch) -> None:
    monkeypatch, tmp_path = clean_env
    client, _calls = _bootstrap_client(monkeypatch, "locked")

    response = client.post(
        "/bootstrap/setup",
        json={"password": "a", "password_confirm": "a", "data_dir": str(tmp_path / "elsewhere")},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "data_root_requires_setup"
    assert dr.pointer_value() == ""


def test_dir_picker_flag_follows_platform_and_tkinter(monkeypatch) -> None:
    import main.api.bootstrap_rest as bootstrap_rest

    monkeypatch.setattr(bootstrap_rest.os, "name", "posix")
    assert bootstrap_rest.dir_picker_available() is False

    monkeypatch.setattr(bootstrap_rest.os, "name", "nt")
    assert bootstrap_rest.dir_picker_available() is True

    def _boom(*args, **kwargs):
        raise ImportError("no tkinter in this image")

    monkeypatch.setitem(__import__("sys").modules, "tkinter", None)
    assert bootstrap_rest.dir_picker_available() is False


def test_pick_endpoint_reports_availability_in_status(clean_env, monkeypatch) -> None:
    import main.api.bootstrap_rest as bootstrap_rest

    monkeypatch, tmp_path = clean_env
    client, _calls = _bootstrap_client(monkeypatch, "setup")

    payload = client.get("/bootstrap/status").json()["item"]

    assert payload["dir_picker"] == {"available": bootstrap_rest.dir_picker_available()}


def test_pick_endpoint_returns_picked_path(clean_env, monkeypatch) -> None:
    import main.api.bootstrap_rest as bootstrap_rest

    monkeypatch, tmp_path = clean_env
    client, _calls = _bootstrap_client(monkeypatch, "setup")
    monkeypatch.setattr(bootstrap_rest, "_ask_directory", lambda: str(tmp_path / "picked"))

    response = client.post("/bootstrap/pick-data-dir")

    assert response.status_code == 200
    assert response.json()["item"] == {"path": str(tmp_path / "picked"), "cancelled": False}


def test_pick_endpoint_reports_cancel(clean_env, monkeypatch) -> None:
    import main.api.bootstrap_rest as bootstrap_rest

    monkeypatch, tmp_path = clean_env
    client, _calls = _bootstrap_client(monkeypatch, "setup")
    monkeypatch.setattr(bootstrap_rest, "_ask_directory", lambda: "")

    response = client.post("/bootstrap/pick-data-dir")

    assert response.status_code == 200
    assert response.json()["item"] == {"path": "", "cancelled": True}


def test_pick_endpoint_refuses_after_setup(clean_env, monkeypatch) -> None:
    monkeypatch, tmp_path = clean_env
    client, _calls = _bootstrap_client(monkeypatch, "locked")

    response = client.post("/bootstrap/pick-data-dir")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "data_root_requires_setup"


def test_pick_endpoint_single_flights_dialogs(clean_env, monkeypatch) -> None:
    import main.api.bootstrap_rest as bootstrap_rest

    monkeypatch, tmp_path = clean_env
    client, _calls = _bootstrap_client(monkeypatch, "setup")
    assert bootstrap_rest._DIR_PICKER_LOCK.acquire(blocking=False)
    try:
        response = client.post("/bootstrap/pick-data-dir")
    finally:
        bootstrap_rest._DIR_PICKER_LOCK.release()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "dir_picker_busy"


def test_pick_endpoint_degrades_when_unavailable(clean_env, monkeypatch) -> None:
    import main.api.bootstrap_rest as bootstrap_rest

    monkeypatch, tmp_path = clean_env
    client, _calls = _bootstrap_client(monkeypatch, "setup")
    monkeypatch.setattr(bootstrap_rest, "dir_picker_available", lambda: False)

    response = client.post("/bootstrap/pick-data-dir")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "dir_picker_unavailable"


def test_temp_anchors_follow_data_root_when_it_diverges(clean_env) -> None:
    """两条工具车道的规范临时根一起挂数据根。

    换锚前这些位置各自拼 `workspace/'temp'`，设了自定义数据目录的安装里只有
    `temp/tasks` 走，`temp/ceo` 与 exec/filesystem 的临时根仍留在安装根。
    """
    monkeypatch, tmp_path = clean_env
    data_dir = tmp_path / "D-data"
    dr.write_data_root_pointer(data_dir)
    workspace = tmp_path / "install"
    workspace.mkdir()

    from g3ku.agent.tools.filesystem_mutation import FilesystemTool
    from g3ku.agent.tools.shell import ExecTool
    from g3ku.runtime.frontdoor.session_temp_dir import ceo_session_temp_dir

    exec_tool = ExecTool(workspace_root=str(workspace))
    fs_tool = FilesystemTool(workspace=workspace)
    data_temp = (data_dir / "temp").resolve()

    assert exec_tool._temp_root() == data_temp
    assert fs_tool._temp_root() == data_temp
    assert data_temp != (workspace / "temp").resolve()
    assert Path(ceo_session_temp_dir("web:ceo-1")) == data_dir / "temp" / "ceo" / "web_ceo-1"
    # 运行时的第二处临时落点（`.g3ku/tmp`）也换根，且仍在拒写名单里。
    assert dr.data_g3ku_path("tmp") in exec_tool._legacy_temp_roots()


def test_temp_dir_outside_workspace_admitted_only_under_temp(clean_env) -> None:
    """准入判据放开的是 `<数据根>/temp/**`，不是整个数据根。

    这两处原本是静默丢弃门：不在项目根内就把 `task_temp_dir` 当没给、退回
    `<项目根>/temp`，写入照样成功但落点不动，所以必须逐值钉住。
    """
    monkeypatch, tmp_path = clean_env
    data_dir = tmp_path / "D-data"
    dr.write_data_root_pointer(data_dir)
    workspace = tmp_path / "install"
    workspace.mkdir()

    from g3ku.agent.tools.filesystem_mutation import FilesystemTool
    from g3ku.agent.tools.shell import ExecTool

    exec_tool = ExecTool(workspace_root=str(workspace))
    fs_tool = FilesystemTool(workspace=workspace)
    data_temp = (data_dir / "temp").resolve()

    for inside in (data_dir / "temp" / "ceo" / "web_ceo-1", data_dir / "temp" / "tasks" / "task_abc"):
        runtime = {"task_temp_dir": str(inside)}
        assert exec_tool._runtime_task_temp_root(runtime) == inside.resolve()
        assert fs_tool._canonical_temp_root(runtime) == inside.resolve()

    for outside in (data_dir / ".g3ku" / "main-runtime", workspace.parent / "elsewhere"):
        runtime = {"task_temp_dir": str(outside)}
        assert exec_tool._runtime_task_temp_root(runtime) is None
        assert fs_tool._canonical_temp_root(runtime) == data_temp

    # 存量 meta 里仍写着安装根 temp 的任务照旧可用（判据是"或"，不是替换）。
    legacy = workspace / "temp" / "tasks" / "task_old"
    runtime = {"task_temp_dir": str(legacy)}
    assert exec_tool._runtime_task_temp_root(runtime) == legacy.resolve()
    assert fs_tool._canonical_temp_root(runtime) == legacy.resolve()
