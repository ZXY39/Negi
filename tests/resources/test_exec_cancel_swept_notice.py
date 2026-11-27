from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import g3ku.agent.tools.shell as shell_module
from g3ku.agent.tools.shell import ExecTool
from g3ku.deployment import data_root as data_root_module
from g3ku.runtime import owned_processes
from g3ku.runtime.tool_jobs import build_swept_process_event_payload
from g3ku.utils.process_tree import ProcessTreeKillResult


@pytest.fixture()
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("G3KU_DATA_DIR", str(tmp_path))
    data_root_module.reset_cache()
    try:
        yield tmp_path
    finally:
        data_root_module.reset_cache()


class _StubProcess:
    pid = 4242
    returncode = None

    def kill(self) -> None:
        return None

    async def wait(self):
        return None


@pytest.mark.asyncio
async def test_cancelled_exec_writes_one_swept_notice(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = ExecTool(workspace_root=str(tmp_path))

    async def _raise_cancel(process, **_kwargs):
        raise asyncio.CancelledError()

    async def _fake_spawn(*_args, **_kwargs):
        return _StubProcess()

    def _fake_kill(_process):
        return ProcessTreeKillResult(
            initiated=True,
            terminated=True,
            descendant_total=2,
            descendants=[
                {"pid": 111, "name": "python.exe", "create_time": 1.0},
                {"pid": 222, "name": "msedge.exe", "create_time": 2.0},
            ],
        )

    monkeypatch.setattr(tool, "_collect_process_output", _raise_cancel)
    monkeypatch.setattr(shell_module.asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(shell_module, "kill_process_tree_checked", _fake_kill)
    monkeypatch.setattr(shell_module.os, "name", "nt", raising=False)

    with pytest.raises(asyncio.CancelledError):
        await tool.execute(
            command='& "py" "watcher.py"',
            __g3ku_runtime={"session_key": "web:ceo-1", "actor_role": "ceo"},
        )

    notices = owned_processes.list_notices()
    assert len(notices) == 1
    notice = notices[0]
    assert notice["swept"] is True
    assert [item["pid"] for item in notice["swept_pids"]] == [111, 222]
    assert notice["cmd"] == '& "py" "watcher.py"'
    assert notice["session_key"] == "web:ceo-1"
    assert notice["exit_reason"] == "swept_by_exec_cancel"

    payload = build_swept_process_event_payload(notice, session_id="web:ceo-1")
    assert payload["job_id"] == f"proc:swept:{notice['sweep_id']}"
    assert payload["dedupe_key"].endswith(":interrupted")
    assert "pid=111" in payload["result_preview"]


@pytest.mark.asyncio
async def test_cancel_without_descendants_writes_no_notice(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool = ExecTool(workspace_root=str(tmp_path))

    async def _raise_cancel(process, **_kwargs):
        raise asyncio.CancelledError()

    async def _fake_spawn(*_args, **_kwargs):
        return _StubProcess()

    monkeypatch.setattr(tool, "_collect_process_output", _raise_cancel)
    monkeypatch.setattr(shell_module.asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(
        shell_module,
        "kill_process_tree_checked",
        lambda _process: ProcessTreeKillResult(initiated=True, terminated=True),
    )
    monkeypatch.setattr(shell_module.os, "name", "nt", raising=False)

    with pytest.raises(asyncio.CancelledError):
        await tool.execute(
            command="echo hello",
            __g3ku_runtime={"session_key": "web:ceo-1"},
        )

    assert owned_processes.list_notices() == []


def test_swept_notice_text_names_the_processes() -> None:
    text = owned_processes.swept_notice_text(
        {
            "cmd": "python watcher.py",
            "swept_pids": [
                {"pid": 111, "name": "python.exe"},
                {"pid": 222, "name": ""},
            ],
        }
    )
    assert "2 process(es)" in text
    assert "Command: python watcher.py" in text
    assert "Swept: pid=111 (python.exe), pid=222." in text
    assert "background=true" in text
