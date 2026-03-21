from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any

from g3ku.china_bridge.client import ChinaBridgeClient
from g3ku.china_bridge.models import ChinaBridgeState
from g3ku.china_bridge.status import ChinaBridgeStatusStore


class _BuildRetryPending(Exception):
    """Internal sentinel used to delay repeated china bridge build attempts."""


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
        self._next_build_attempt_at = 0.0
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
            try:
                dist_entry = await self._ensure_host_build()
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
            except _BuildRetryPending:
                continue
            except Exception as exc:
                self._write_state(running=False, connected=False, pid=None, last_error=str(exc))
                await asyncio.sleep(1.0)

    def _host_root(self) -> Path:
        return self._workspace / "subsystems" / "china_channels_host"

    def _dist_entry(self) -> Path:
        return self._host_root() / "dist" / "index.js"

    def _node_modules_dir(self) -> Path:
        return self._host_root() / "node_modules"

    def _resolve_package_manager(self) -> tuple[str, str]:
        preferred = str(self._app_config.china_bridge.npm_client or "pnpm").strip() or "pnpm"
        candidates: list[str] = [preferred]
        for fallback in ("pnpm", "npm"):
            if fallback not in candidates:
                candidates.append(fallback)
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved, candidate
        raise RuntimeError(
            "china bridge build requires a package manager, but none of these were found: "
            + ", ".join(candidates)
        )

    def _latest_host_source_mtime(self) -> float:
        host_root = self._host_root()
        latest = 0.0
        for relative in ("package.json", "tsconfig.json", "upstream_map.yaml"):
            path = host_root / relative
            if path.exists():
                latest = max(latest, path.stat().st_mtime)
        src_root = host_root / "src"
        if src_root.exists():
            for path in src_root.rglob("*.ts"):
                if path.is_file():
                    latest = max(latest, path.stat().st_mtime)
        return latest

    def _host_build_required(self) -> bool:
        dist_entry = self._dist_entry()
        if not dist_entry.exists():
            return True
        try:
            dist_mtime = dist_entry.stat().st_mtime
        except OSError:
            return True
        return self._latest_host_source_mtime() > dist_mtime

    def _trim_process_output(self, stdout: bytes, stderr: bytes) -> str:
        combined = "\n".join(
            part.strip()
            for part in (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )
            if part.strip()
        ).strip()
        if not combined:
            return ""
        if len(combined) <= 2000:
            return combined
        return combined[-2000:]

    async def _run_host_command(self, *args: str) -> None:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self._host_root()),
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            return
        tail = self._trim_process_output(stdout, stderr)
        detail = f"; output={tail}" if tail else ""
        raise RuntimeError(f"command failed ({process.returncode}): {' '.join(args)}{detail}")

    async def _ensure_host_build(self) -> Path:
        dist_entry = self._dist_entry()
        if not self._host_build_required():
            self._write_state(built=True)
            return dist_entry

        now = time.monotonic()
        if now < self._next_build_attempt_at:
            wait_seconds = max(0.1, self._next_build_attempt_at - now)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            raise _BuildRetryPending()

        package_manager_path, package_manager_name = self._resolve_package_manager()

        try:
            if not self._node_modules_dir().exists():
                self._write_state(
                    running=False,
                    connected=False,
                    built=False,
                    pid=None,
                    last_error=f"installing china bridge dependencies via {package_manager_name} install",
                )
                install_args = [package_manager_path, "install"]
                if package_manager_name == "npm":
                    install_args.append("--no-package-lock")
                await self._run_host_command(*install_args)

            self._write_state(
                running=False,
                connected=False,
                built=False,
                pid=None,
                last_error=f"building china bridge host via {package_manager_name} run build",
            )
            await self._run_host_command(package_manager_path, "run", "build")
        except Exception:
            self._next_build_attempt_at = time.monotonic() + 10.0
            raise

        if not dist_entry.exists():
            self._next_build_attempt_at = time.monotonic() + 10.0
            raise RuntimeError(f"china bridge build finished but output missing: {dist_entry}")

        self._next_build_attempt_at = 0.0
        self._write_state(built=True, last_error="")
        return dist_entry

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
