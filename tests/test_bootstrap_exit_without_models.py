from typing import Any

import pytest


async def test_running_work_snapshot_is_empty_when_runtime_cannot_build(monkeypatch):
    """没配模型时 get_agent() 会抛；退出、优雅重启与「重启并更新」共用这个快照，
    抛出去就是 500，那类设备永远关不掉自己。"""
    import main.api.bootstrap_rest as bootstrap_api

    def _boom() -> Any:
        raise ValueError("No model configured for role 'ceo'.")

    monkeypatch.setattr(bootstrap_api, "get_agent", _boom)
    monkeypatch.setattr(bootstrap_api, "_assert_unlocked", lambda: None)

    snapshot = await bootstrap_api._running_work_snapshot()
    assert snapshot["has_running_work"] is False
    assert snapshot["running_sessions"] == []
    assert snapshot["running_tasks"] == []
    assert snapshot["summary_text"]
    assert "No model configured" in snapshot["runtime_unavailable"]


async def test_exit_endpoint_shuts_down_instead_of_500_without_models(monkeypatch):
    import main.api.bootstrap_rest as bootstrap_api

    def _boom() -> Any:
        raise ValueError("No model configured for role 'ceo'.")

    calls: list[str] = []

    async def _shutdown_runtime() -> None:
        calls.append("runtime")

    def _request_shutdown() -> bool:
        calls.append("server")
        return True

    monkeypatch.setattr(bootstrap_api, "get_agent", _boom)
    monkeypatch.setattr(bootstrap_api, "_assert_unlocked", lambda: None)
    monkeypatch.setattr(bootstrap_api, "shutdown_web_runtime", _shutdown_runtime)
    monkeypatch.setattr(bootstrap_api, "request_server_shutdown", _request_shutdown)

    result = await bootstrap_api.bootstrap_exit({"pause_running_work": False})
    assert result["ok"] is True
    assert result["item"]["shutting_down"] is True
    assert calls == ["runtime", "server"]


async def test_unlocked_assertion_still_gates_the_snapshot(monkeypatch):
    """放宽的是"构造不出运行时"，不是锁状态：没解锁仍然要 423。"""
    from fastapi import HTTPException

    import main.api.bootstrap_rest as bootstrap_api

    def _locked() -> None:
        raise HTTPException(status_code=423, detail="project_locked")

    monkeypatch.setattr(bootstrap_api, "_assert_unlocked", _locked)
    with pytest.raises(HTTPException) as exc:
        await bootstrap_api._running_work_snapshot()
    assert exc.value.status_code == 423
