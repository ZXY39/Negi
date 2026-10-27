"""每会话一个 jail 的生命周期与目录布局。

粒度是**会话**：同一个会话的多次调用共用一个卷（文件连续），不同会话绝不共用——
跨群共享等于跨群泄露。没有常驻进程，所以"生命周期"只是目录准备、lease 计数与
idle 回收；jail 本身每次调用现建现拆（bwrap 的 namespace 本来就是一次性的）。

三种布局：

* ``local``（Linux / macOS）：卷在数据根 ``sandboxes/<id>/`` 下，宿主直读；
* ``wsl``（Windows）：卷在发行版的 ``~/.negi-sandbox/sandboxes/<id>/`` 下，
  宿主侧看不到，导入导出走 ``wsl`` 的文件桥；
* ``container``：卷同 ``local``，挂进容器。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from g3ku.sandbox.backend_base import (
    BackendSelection,
    Enforcement,
    ExecResult,
    SandboxBackend,
    SandboxSpec,
)
from g3ku.sandbox.errors import (
    KIND_DENIED,
    KIND_RUNNER_FAILURE,
    KIND_UNAVAILABLE,
    SandboxDeniedError,
    SandboxUnavailableError,
    classify_result,
)
from g3ku.sandbox.profiles import JAIL_WORKSPACE
from g3ku.utils.helpers import safe_filename

FSOP_HELPER_NAME = "fsop.py"
FSOP_JAIL_PATH = "/opt/negi/fsop.py"
_DEFAULT_EXEC_TIMEOUT_SECONDS = 300.0
_MAX_EXEC_TIMEOUT_SECONDS = 3600.0


def sandbox_id_for(session_key: str) -> str:
    return safe_filename(str(session_key or "").replace(":", "_")).strip("._") or "session"


@dataclass(slots=True)
class SandboxHandle:
    session_key: str
    spec: SandboxSpec
    backend: SandboxBackend
    paths: dict[str, str]
    created_at: float = field(default_factory=time.time)

    @property
    def sandbox_id(self) -> str:
        return self.spec.sandbox_id


class _LocalLayout:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def paths_for(self, sandbox_id: str) -> dict[str, Path]:
        root = self.base_dir / sandbox_id
        return {
            "workspace": root / "workspace",
            "inbox": root / "shared" / "inbox",
            "outbox": root / "shared" / "outbox",
        }

    def ensure(self, sandbox_id: str) -> dict[str, Path]:
        paths = self.paths_for(sandbox_id)
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths

    def destroy(self, sandbox_id: str) -> None:
        root = self.base_dir / sandbox_id
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)


class _WslLayout:
    """卷在发行版内部：`/mnt/*` 既慢又属于禁绑前缀，工作区必须落在 ext4 上。"""

    def __init__(self, *, wsl_path: str, distro: str):
        self.wsl_path = wsl_path
        self.distro = distro
        self._helper_ready = False

    def _base(self) -> str:
        return "~/.negi-sandbox/sandboxes"

    def paths_for(self, sandbox_id: str) -> dict[str, str]:
        root = f"{self._base()}/{sandbox_id}"
        return {
            "workspace": f"{root}/workspace",
            "inbox": f"{root}/shared/inbox",
            "outbox": f"{root}/shared/outbox",
        }

    def _run(self, script: str, *, timeout: float = 60.0) -> tuple[int, str]:
        completed = subprocess.run(  # noqa: S603 - 固定 argv
            [self.wsl_path, "-d", self.distro, "--", "sh", "-lc", script],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, (completed.stdout or b"").decode("utf-8", errors="replace")

    def ensure(self, sandbox_id: str) -> dict[str, str]:
        paths = self.paths_for(sandbox_id)
        script = "; ".join(f"mkdir -p ~/{path.lstrip('~').lstrip('/')}" for path in paths.values())
        code, _ = self._run(script)
        if code != 0:
            raise SandboxUnavailableError(f"wsl mkdir failed in {self.distro}")
        self._install_helper()
        return paths

    def _install_helper(self) -> None:
        if self._helper_ready:
            return
        source = Path(__file__).with_name(FSOP_HELPER_NAME)
        if not source.is_file():
            raise SandboxUnavailableError("fsop helper missing")
        script = f"mkdir -p ~/.negi-sandbox && cat > ~/.negi-sandbox/{FSOP_HELPER_NAME}"
        completed = subprocess.run(  # noqa: S603
            [self.wsl_path, "-d", self.distro, "--", "sh", "-lc", script],
            input=source.read_bytes(),
            capture_output=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise SandboxUnavailableError("wsl helper copy failed")
        self._helper_ready = True

    @property
    def helper_path(self) -> str:
        return f"~/.negi-sandbox/{FSOP_HELPER_NAME}"

    def destroy(self, sandbox_id: str) -> None:
        self._run(f"rm -rf ~/.negi-sandbox/sandboxes/{sandbox_id}", timeout=120)


class SandboxManager:
    def __init__(
        self,
        *,
        base_dir: Path | None,
        backends: Sequence[SandboxBackend],
        install_reading: dict[str, Any] | None = None,
    ):
        self._backends = list(backends)
        self._install = dict(install_reading or {})
        self._lock = threading.RLock()
        self._handles: dict[str, SandboxHandle] = {}
        self._leases: dict[str, int] = {}
        self._last_used: dict[str, float] = {}
        self._local = _LocalLayout(Path(base_dir)) if base_dir is not None else None
        self._wsl: _WslLayout | None = None

    # -- 选择 ---------------------------------------------------------------

    def select(self, *, trust: str) -> BackendSelection:
        """按信任档挑后端：第三方要求必需维度全 full，owner 允许 partial。"""
        for backend in self._backends:
            enforcement = backend.enforcement()
            if trust != "owner" and not enforcement.satisfies_required():
                continue
            return BackendSelection(
                backend=backend,
                name=backend.name,
                reason="",
                detail={"install_state": self._install.get("state", "")},
                enforcement=enforcement,
                third_party_usable=enforcement.satisfies_required(),
            )
        return BackendSelection(
            backend=None,
            name="",
            reason=str(self._install.get("reason") or "no_backend"),
            detail={"install_state": self._install.get("state", "")},
            enforcement=Enforcement(),
        )

    def available(self, *, trust: str) -> bool:
        return self.select(trust=trust).backend is not None

    # -- 生命周期 -----------------------------------------------------------

    def ensure(
        self,
        session_key: str,
        *,
        trust: str,
        network: bool = False,
        read_roots: Sequence[Path] = (),
        write_roots: Sequence[Path] = (),
    ) -> SandboxHandle:
        key = str(session_key or "").strip()
        if not key:
            raise SandboxUnavailableError("session key is required")
        with self._lock:
            existing = self._handles.get(key)
            if existing is not None:
                self._last_used[key] = time.time()
                return existing

            selection = self.select(trust=trust)
            if selection.backend is None:
                raise SandboxUnavailableError(
                    f"sandbox backend unavailable: {selection.reason}",
                    detail={"install_state": self._install.get("state", "")},
                )
            sandbox_id = sandbox_id_for(key)
            backend = selection.backend
            helper_binds: tuple[tuple[Path, str], ...] = ()
            if backend.name == "wsl_bwrap":
                if self._wsl is None:
                    raise SandboxUnavailableError("wsl layout is not configured")
                wsl_paths = self._wsl.ensure(sandbox_id)
                paths = dict(wsl_paths)
                workspace = Path(wsl_paths["workspace"])
                inbox = Path(wsl_paths["inbox"])
                outbox = Path(wsl_paths["outbox"])
            else:
                if self._local is None:
                    raise SandboxUnavailableError("local sandbox layout is not configured")
                local_paths = self._local.ensure(sandbox_id)
                workspace = local_paths["workspace"].resolve()
                inbox = local_paths["inbox"].resolve()
                outbox = local_paths["outbox"].resolve()
                helper_source = Path(__file__).with_name(FSOP_HELPER_NAME)
                helper_binds = ((helper_source, FSOP_JAIL_PATH),)
                paths = {name: str(path) for name, path in local_paths.items()}

            spec = SandboxSpec(
                sandbox_id=sandbox_id,
                session_key=key,
                workspace_root=workspace,
                shared_inbox=inbox,
                shared_outbox=outbox,
                read_roots=tuple(Path(item) for item in read_roots),
                write_roots=tuple(Path(item) for item in write_roots),
                network=bool(network),
                trust=str(trust or ""),
                helper_binds=helper_binds,
            )
            backend.create(spec)
            handle = SandboxHandle(session_key=key, spec=spec, backend=backend, paths=paths)
            self._handles[key] = handle
            self._leases.setdefault(key, 0)
            self._last_used[key] = time.time()
            return handle

    def handle(self, session_key: str) -> SandboxHandle | None:
        with self._lock:
            return self._handles.get(str(session_key or "").strip())

    def acquire(self, session_key: str) -> None:
        key = str(session_key or "").strip()
        with self._lock:
            self._leases[key] = int(self._leases.get(key, 0)) + 1
            self._last_used[key] = time.time()

    def release(self, session_key: str) -> None:
        key = str(session_key or "").strip()
        with self._lock:
            current = int(self._leases.get(key, 0))
            self._leases[key] = max(0, current - 1)
            self._last_used[key] = time.time()

    def forget(self, session_key: str) -> None:
        key = str(session_key or "").strip()
        with self._lock:
            handle = self._handles.pop(key, None)
            self._leases.pop(key, None)
            self._last_used.pop(key, None)
        if handle is None:
            return
        try:
            handle.backend.destroy(handle.spec)
        except Exception:  # noqa: BLE001 - 拆除失败不影响会话删除
            pass
        if handle.backend.name == "wsl_bwrap" and self._wsl is not None:
            self._wsl.destroy(handle.sandbox_id)
        elif self._local is not None:
            self._local.destroy(handle.sandbox_id)

    def reap_idle(self, *, max_idle_seconds: float, now: float | None = None) -> list[str]:
        """回收空闲 jail（目录保留、handle 丢弃）。有 lease 的一律不动。"""
        moment = float(now if now is not None else time.time())
        reaped: list[str] = []
        with self._lock:
            for key, used_at in list(self._last_used.items()):
                if int(self._leases.get(key, 0)) > 0:
                    continue
                if moment - used_at < float(max_idle_seconds):
                    continue
                handle = self._handles.pop(key, None)
                self._leases.pop(key, None)
                self._last_used.pop(key, None)
                if handle is not None:
                    reaped.append(key)
        return reaped

    # -- 执行 ---------------------------------------------------------------

    def run(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        cwd: str = JAIL_WORKSPACE,
        timeout: float = _DEFAULT_EXEC_TIMEOUT_SECONDS,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        wrapped = handle.backend.build_argv(handle.spec, list(argv), cwd=cwd, env=env)
        bounded = max(1.0, min(float(timeout or _DEFAULT_EXEC_TIMEOUT_SECONDS), _MAX_EXEC_TIMEOUT_SECONDS))
        with self._lock:
            self._last_used[handle.session_key] = time.time()
        try:
            completed = subprocess.run(  # noqa: S603 - argv 由后端生成，不拼 shell
                wrapped,
                capture_output=True,
                timeout=bounded,
                check=False,
                cwd=None,
                env=_subprocess_env(handle),
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                exit_code=None,
                stdout=_decode(exc.stdout),
                stderr=f"sandbox command timed out after {bounded:.0f}s",
                kind=KIND_UNAVAILABLE,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailableError(f"sandbox backend not runnable: {exc}") from exc
        stdout = _decode(completed.stdout)
        stderr = _decode(completed.stderr)
        kind = classify_result(exit_code=completed.returncode, stderr=stderr)
        if kind == KIND_RUNNER_FAILURE:
            raise SandboxUnavailableError(f"sandbox backend failed to start: {stderr.strip()[:300]}")
        return ExecResult(exit_code=completed.returncode, stdout=stdout, stderr=stderr, kind=kind)

    def fsop(
        self,
        handle: SandboxHandle,
        request: dict[str, Any],
        *,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        import json

        helper = (
            self._wsl.helper_path
            if handle.backend.name == "wsl_bwrap" and self._wsl is not None
            else FSOP_JAIL_PATH
        )
        payload = json.dumps(request, ensure_ascii=False)
        argv = [
            "python3",
            helper,
            "--root",
            str(handle.spec.workspace_root),
            "--root",
            str(handle.spec.shared_inbox),
            "--root",
            str(handle.spec.shared_outbox),
        ]
        result = self._run_with_stdin(handle, argv, payload, timeout=timeout)
        if result.kind == KIND_DENIED or result.exit_code == 2:
            try:
                parsed = json.loads(result.stdout.strip().splitlines()[-1])
            except (IndexError, ValueError):
                parsed = {}
            if parsed.get("kind") == "denied":
                raise SandboxDeniedError(str(parsed.get("error") or "sandbox denied"), detail=parsed)
            return dict(parsed or {"ok": False, "kind": "invalid", "error": result.stderr[-300:]})
        try:
            return json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise SandboxUnavailableError(f"fsop returned unparsable output: {exc}") from exc

    def _run_with_stdin(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        stdin_text: str,
        *,
        timeout: float,
    ) -> ExecResult:
        wrapped = handle.backend.build_argv(handle.spec, list(argv), cwd=JAIL_WORKSPACE)
        try:
            completed = subprocess.run(  # noqa: S603
                wrapped,
                input=stdin_text.encode("utf-8"),
                capture_output=True,
                timeout=max(1.0, min(float(timeout), _MAX_EXEC_TIMEOUT_SECONDS)),
                check=False,
                env=_subprocess_env(handle),
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailableError(f"sandbox backend not runnable: {exc}") from exc
        except subprocess.TimeoutExpired:
            return ExecResult(exit_code=None, stderr="fsop timed out", kind=KIND_UNAVAILABLE, timed_out=True)
        stdout = _decode(completed.stdout)
        stderr = _decode(completed.stderr)
        kind = classify_result(exit_code=completed.returncode, stderr=stderr)
        return ExecResult(exit_code=completed.returncode, stdout=stdout, stderr=stderr, kind=kind)

    # -- 读数 ---------------------------------------------------------------

    def status(self, session_key: str | None = None) -> dict[str, Any]:
        with self._lock:
            if session_key is None:
                return {
                    "backend": "",
                    "sandboxes": len(self._handles),
                    "leases": dict(self._leases),
                }
            key = str(session_key).strip()
            handle = self._handles.get(key)
            if handle is None:
                return {"session_key": key, "active": False}
            return {
                "session_key": key,
                "active": True,
                "sandbox_id": handle.sandbox_id,
                "backend": handle.backend.name,
                "paths": dict(handle.paths),
                "lease": int(self._leases.get(key, 0)),
                "idle_seconds": round(time.time() - float(self._last_used.get(key, time.time())), 3),
                "enforcement": handle.backend.enforcement().as_dict(),
            }


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return bytes(value).decode("utf-8", errors="replace")
    except (TypeError, ValueError):
        return str(value)


def _subprocess_env(handle: SandboxHandle) -> dict[str, str] | None:
    """本地后端的宿主环境要剥干净；wsl.exe 需要宿主环境才能启动。"""
    if bool(getattr(handle.backend, "keeps_host_env", False)):
        return None
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": JAIL_WORKSPACE,
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "SANDBOX_ID": handle.sandbox_id,
    }


__all__ = [
    "FSOP_HELPER_NAME",
    "FSOP_JAIL_PATH",
    "SandboxHandle",
    "SandboxManager",
    "sandbox_id_for",
]
