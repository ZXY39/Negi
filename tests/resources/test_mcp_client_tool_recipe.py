"""Live roundtrip for the MCP-client-tool recipe (skills/add-tool/references/mcp-client-tool.md).

The recipe tells an agent to wrap a foreign MCP server inside an ordinary internal
tool. This pins the two things that recipe claims about the SDK: the stdio
handshake/call sequence works in this project's pinned `mcp<2` environment, and a
cancelled tool call does not leave the server child process running.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_SOURCE = '''
import atexit
import json
import os
import threading
import time

from mcp.server.fastmcp import FastMCP

TRACE_PATH = os.environ["RECIPE_TRACE_PATH"]
PID = os.getpid()


def _beat():
    while True:
        try:
            with open(TRACE_PATH, "w", encoding="utf-8") as handle:
                json.dump({"pid": PID, "beat": time.time()}, handle)
        except Exception:
            return
        time.sleep(0.1)


threading.Thread(target=_beat, daemon=True).start()

server = FastMCP("recipe-probe")


@server.tool()
def echo_probe(text: str) -> str:
    return "probe:" + text


@server.tool()
def slow_probe(seconds: float) -> str:
    time.sleep(seconds)
    return "slept"


if __name__ == "__main__":
    server.run(transport="stdio")
'''


def _start_server(tmp_path: Path) -> tuple[StdioServerParameters, Path]:
    script = tmp_path / 'mcp_recipe_server.py'
    script.write_text(SERVER_SOURCE, encoding='utf-8')
    trace_path = tmp_path / 'heartbeat.json'
    trace_path.write_text(json.dumps({'pid': 0, 'beat': 0.0}), encoding='utf-8')
    env = dict(os.environ)
    env['RECIPE_TRACE_PATH'] = str(trace_path)
    params = StdioServerParameters(command=sys.executable, args=[str(script)], env=env)
    return params, trace_path


def _read_trace(trace_path: Path) -> dict:
    try:
        return json.loads(trace_path.read_text(encoding='utf-8'))
    except Exception:
        return {'pid': 0, 'beat': 0.0}


def _pid_alive(pid: int) -> bool:
    import ctypes
    import ctypes.wintypes

    if pid <= 0:
        return False
    process_query_limited_information = 0x1000
    still_active = 259

    class ExitCodeProcess(ctypes.Structure):
        _fields_ = [('hProcess', ctypes.wintypes.HANDLE), ('dwExitCode', ctypes.wintypes.DWORD)]

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        info = ExitCodeProcess()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(info)):
            return True
        return info.dwExitCode == still_active
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.asyncio
async def test_mcp_client_roundtrip_against_local_server(tmp_path: Path) -> None:
    params, _trace = _start_server(tmp_path)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = {item.name for item in (await session.list_tools()).tools}
            assert {'echo_probe', 'slow_probe'} <= listed

            result = await session.call_tool(
                'echo_probe',
                {'text': 'ok'},
                read_timeout_seconds=timedelta(seconds=30),
            )

    assert result.isError is not True
    assert 'probe:ok' in str(result.content[0].text)


@pytest.mark.asyncio
async def test_cancelled_call_does_not_orphan_the_server_child(tmp_path: Path) -> None:
    params, trace_path = _start_server(tmp_path)

    async def _call() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool(
                    'slow_probe',
                    {'seconds': 30},
                    read_timeout_seconds=timedelta(seconds=60),
                )

    task = asyncio.create_task(_call())
    # Let the child report a live heartbeat before cutting the call short.
    deadline = time.time() + 30
    while _read_trace(trace_path).get('beat', 0.0) == 0.0 and time.time() < deadline:
        await asyncio.sleep(0.1)
    started = _read_trace(trace_path)
    assert started.get('pid', 0) > 0, 'server child never started'

    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The child must stop beating and be gone; give it a bounded window so the
    # observation is about cleanup, not about scheduling jitter.
    settle_deadline = time.time() + 15
    last_beat = started['beat']
    while time.time() < settle_deadline:
        current = _read_trace(trace_path)
        last_beat = max(last_beat, current.get('beat', 0.0))
        if last_beat > started['beat'] and not _pid_alive(started['pid']):
            break
        await asyncio.sleep(0.2)

    assert not _pid_alive(started['pid']), (
        f'server child pid={started["pid"]} still alive after cancellation; '
        f'last heartbeat {last_beat} vs started {started["beat"]}'
    )


@pytest.mark.asyncio
async def test_recipe_template_shape_runs_as_a_negi_tool(tmp_path: Path) -> None:
    """`ProbeTool` below mirrors the template class in
    `skills/add-tool/references/mcp-client-tool.md` line for line — the doc is prose
    and cannot be imported, so changing the template means changing this too."""
    import asyncio as _asyncio
    from typing import Any

    from g3ku.agent.tools.base import Tool

    def _cancelled(runtime: dict[str, Any]) -> bool:
        token = runtime.get('cancel_token')
        for attr in ('is_cancelled', 'cancelled'):
            flag = getattr(token, attr, None)
            if callable(flag):
                return bool(flag())
            if flag is not None:
                return bool(flag)
        return False

    spawn_floor = ('SystemRoot', 'COMSPEC', 'PATHEXT', 'PATH')

    def _spawn_env(extra: dict[str, str]) -> dict[str, str]:
        env = {key: os.environ[key] for key in spawn_floor if key in os.environ}
        env.update(extra)
        return env

    class ProbeTool(Tool):
        self_enforced_timeout = True
        rerun_safe = False

        def __init__(
            self,
            *,
            tool_name: str,
            description: str,
            server_command: list[str],
            remote_tool: str,
            server_env: dict[str, str] | None = None,
        ) -> None:
            self._tool_name = str(tool_name)
            self._description = str(description)
            self._server_command = list(server_command)
            self._remote_tool = str(remote_tool)
            self._server_env = dict(server_env or {})

        @property
        def name(self) -> str:
            return self._tool_name

        @property
        def description(self) -> str:
            return self._description

        @property
        def parameters(self) -> dict[str, Any]:
            return {
                'type': 'object',
                'properties': {'text': {'type': 'string', 'description': '要回显的文本'}},
                'required': ['text'],
            }

        async def execute(self, timeout_seconds: float | None = None, **kwargs: Any) -> str:
            runtime = kwargs.pop('__g3ku_runtime', None) or {}
            arguments = dict(kwargs)
            limit = max(1.0, float(timeout_seconds or 30.0))
            params = StdioServerParameters(
                command=self._server_command[0],
                args=self._server_command[1:],
                env=_spawn_env(self._server_env),
            )
            call = _asyncio.ensure_future(self._call(params, limit, runtime, arguments))
            done, _pending = await _asyncio.wait({call}, timeout=limit)
            if not done:
                # stdio_client 的清理在取消时会把自己的 BrokenResourceError 包成
                # ExceptionGroup 抛出来，那会把 wait_for 的 TimeoutError 顶掉。
                # 自己收口：取消后把清理异常吃掉，超时仍然是一条普通错误回执。
                call.cancel()
                try:
                    await call
                except (_asyncio.CancelledError, BaseExceptionGroup):
                    pass
                return f'Error: {self.name} 超过 {limit:.0f}s 未返回，远端调用已被放弃'
            return call.result()

        async def _call(
            self,
            params: StdioServerParameters,
            limit: float,
            runtime: dict[str, Any],
            arguments: dict[str, Any],
        ) -> str:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if _cancelled(runtime):
                        raise _asyncio.CancelledError
                    result = await session.call_tool(
                        self._remote_tool,
                        arguments,
                        read_timeout_seconds=timedelta(seconds=limit),
                    )
            if getattr(result, 'isError', False):
                return f'Error: {self.name} 远端返回: {result.content[0].text}'
            return str(result.content[0].text)

    params, trace = _start_server(tmp_path)
    server_env = {'RECIPE_TRACE_PATH': str(trace)}
    tool = ProbeTool(
        tool_name='echo_probe',
        description='Roundtrip a string through the local MCP server.',
        server_command=[params.command, *params.args],
        remote_tool='echo_probe',
        server_env=server_env,
    )

    assert tool.validate_params({'text': 'ok'}) == []
    # base.Tool injects the universal timeout argument into the model-visible schema
    model_properties = tool.to_model_schema()['function']['parameters']['properties']
    assert 'timeout_seconds' in model_properties
    assert ProbeTool.self_enforced_timeout is True
    assert ProbeTool.rerun_safe is False

    assert await tool.execute(text='ok') == 'probe:ok'

    slow = ProbeTool(
        tool_name='slow_probe',
        description='Sleeps on the server side.',
        server_command=[params.command, *params.args],
        remote_tool='slow_probe',
        server_env=server_env,
    )
    timed_out = await slow.execute(seconds=30, timeout_seconds=1.0)
    assert timed_out.startswith('Error:')
    assert '超过 1s' in timed_out
