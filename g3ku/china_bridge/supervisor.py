from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from g3ku.china_bridge.client import ChinaBridgeClient
from g3ku.china_bridge.models import ChinaBridgeState
from g3ku.china_bridge.status import ChinaBridgeStatusStore


class ChinaBridgeSupervisor:
    def __init__(
        self,
        *,
        app_config: Any,
        workspace: Path,
        transport,
    ):
        self._app_config = app_config
        self._workspace = Path(workspace)
        self._transport = transport
        state_root = self._workspace / str(app_config.china_bridge.state_dir or ".g3ku/china-bridge")
        self._state_root = state_root
        self._status_store = ChinaBridgeStatusStore(state_root / "status.json")
        self._runner_task: asyncio.Task | None = None
        self._client_task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stop = asyncio.Event()
        self._state = ChinaBridgeState(
            enabled=bool(app_config.china_bridge.enabled),
            public_port=int(app_config.china_bridge.public_port),
            control_port=int(app_config.china_bridge.control_port),
        )

    @property
    def state(self) -> ChinaBridgeState:
        return self._state

    async def start(self) -> None:
        if not bool(self._app_config.china_bridge.enabled) or not bool(self._app_config.china_bridge.auto_start):
            self._write_state(running=False, connected=False)
            return
        if self._runner_task is not None:
            return
        self._stop.clear()
        self._runner_task = asyncio.create_task(self._run_loop())

    async def wait(self) -> None:
        if self._runner_task is not None:
            await self._runner_task

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._client_task is not None:
            self._client_task.cancel()
            await asyncio.gather(self._client_task, return_exceptions=True)
            self._client_task = None
        if self._process is not None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
            self._process = None
        if self._runner_task is not None:
            self._runner_task.cancel()
            await asyncio.gather(self._runner_task, return_exceptions=True)
            self._runner_task = None
        self._write_state(running=False, connected=False, pid=None)

    def status_payload(self) -> dict[str, Any]:
        return self._state.__dict__.copy()

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            dist_entry = self._workspace / "subsystems" / "china_channels_host" / "dist" / "index.js"
            if not dist_entry.exists():
                self._write_state(running=False, connected=False, built=False, last_error=f"missing build output: {dist_entry}")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._spawn_process(dist_entry)
                url = f"ws://{self._app_config.china_bridge.control_host}:{self._app_config.china_bridge.control_port}"
                client = ChinaBridgeClient(
                    url=url,
                    token=str(self._app_config.china_bridge.control_token or ""),
                    on_frame=self._transport.handle_frame,
                    on_state=self._on_client_state,
                )
                self._transport.set_sender(client.send_frame)
                self._client_task = asyncio.create_task(client.run_forever())
                await self._process.wait()
                await asyncio.gather(self._client_task, return_exceptions=True)
                self._client_task = None
                if self._stop.is_set():
                    break
                self._write_state(running=False, connected=False, pid=None, last_error="china bridge host exited")
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._write_state(running=False, connected=False, pid=None, last_error=str(exc))
                await asyncio.sleep(1.0)

    async def _spawn_process(self, dist_entry: Path) -> None:
        config_path = self._workspace / ".g3ku" / "config.json"
        env = os.environ.copy()
        self._process = await asyncio.create_subprocess_exec(
            str(self._app_config.china_bridge.node_bin or "node"),
            str(dist_entry),
            "--config",
            str(config_path),
            cwd=str(dist_entry.parent.parent),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._write_state(running=True, built=True, pid=int(self._process.pid or 0))

    async def _on_client_state(self, connected: bool, reason: str) -> None:
        self._write_state(connected=connected, last_error="" if connected else reason)

    def _write_state(self, **patch: Any) -> None:
        for key, value in patch.items():
            if hasattr(self._state, key):
                setattr(self._state, key, value)
        self._status_store.write(self._state)
