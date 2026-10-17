"""`negi cron run` must reach cron dispatch holding a session runtime bridge.

The command builds its own ``SessionRuntimeManager``/``SessionRuntimeBridge`` pair (the same
shape ``g3ku/shells/cli.py`` uses), but neither name was imported into the CLI module, so a
manual run died on NameError before a single job was dispatched.
"""

from __future__ import annotations

import pytest

from g3ku.cli import commands
from g3ku.runtime import SessionRuntimeBridge


class _Job:
    def __init__(self, job_id: str) -> None:
        self.id = job_id
        self.name = f"job-{job_id}"


class _FakeCronService:
    def __init__(self, store_path) -> None:
        self.store_path = store_path
        self.on_job = None

    async def run_job(self, job_id: str, *, force: bool = False) -> bool:
        if self.on_job is None:
            return False
        await self.on_job(_Job(job_id))
        return True


class _FakeLoop:
    def __init__(self) -> None:
        self.closed = False
        self.sessions = object()

    async def close_mcp(self) -> None:
        self.closed = True


def test_cron_run_constructs_runtime_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from g3ku.bus import queue as bus_queue
    from g3ku.config import loader as config_loader
    from g3ku.cron import runtime_dispatch
    from g3ku.cron import service as cron_service

    loop = _FakeLoop()
    captured: dict[str, object] = {}

    monkeypatch.setattr(config_loader, "load_config", lambda *a, **k: object())
    monkeypatch.setattr(config_loader, "get_data_dir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(bus_queue, "MessageBus", lambda *a, **k: object())
    monkeypatch.setattr(commands, "_make_provider", lambda config, **k: object())
    monkeypatch.setattr(commands, "_make_agent_loop", lambda config, bus, provider, **k: loop)
    monkeypatch.setattr(commands, "_print_agent_response", lambda response, **k: None)
    monkeypatch.setattr(cron_service, "CronService", _FakeCronService)

    async def _fake_dispatch(job, *, runtime_bridge, session_manager=None, register_task=None, **k):
        captured["bridge"] = runtime_bridge
        captured["session_manager"] = session_manager
        captured["job_id"] = job.id
        return "done"

    monkeypatch.setattr(runtime_dispatch, "dispatch_cron_job", _fake_dispatch)

    commands.cron_run(job_id="job-1", force=False)

    assert isinstance(captured.get("bridge"), SessionRuntimeBridge)
    assert captured.get("job_id") == "job-1"
    assert loop.closed is True
