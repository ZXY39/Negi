"""可选容器后端（Podman 优先，Docker 次之）。

不是默认路径：Windows 上 Podman 本身就跑在 WSL2 里，直接用发行版少一层；
但在已装容器运行时的机器上它是现成的强隔离（cap-drop + no-new-privileges + 资源限制）。

约定：inbox 只读挂载、outbox 可写、workspace 可写；``--network none`` 是默认，
开了 egress 也只走 P3 的代理（本模块不提供）。
"""

from __future__ import annotations

from typing import Any, Sequence

from g3ku.sandbox.backend_base import Enforcement, SandboxSpec
from g3ku.sandbox.probe import FULL
from g3ku.sandbox.profiles import JAIL_INBOX, JAIL_OUTBOX, JAIL_WORKSPACE


class ContainerBackend:
    name = "container"
    # 容器自带环境，宿主环境不参与。
    keeps_host_env = False

    def __init__(
        self,
        *,
        runtime: str = "podman",
        image: str = "ghcr.io/negi/sandbox:latest",
        enforcement: Enforcement | None = None,
        pids_limit: int = 512,
        memory: str = "2g",
    ):
        self._runtime = str(runtime or "podman")
        self._image = str(image)
        self._pids = int(pids_limit)
        self._memory = str(memory)
        self._enforcement = enforcement or Enforcement(
            fs_read=FULL,
            fs_write=FULL,
            network=FULL,
            process=FULL,
            resource=FULL,
        )

    def probe(self) -> dict[str, Any]:
        return {
            "state": "ready",
            "backend": self.name,
            "enforcement": self.enforcement().as_dict(),
            "detail": {"runtime": self._runtime, "image": self._image},
        }

    def enforcement(self) -> Enforcement:
        return self._enforcement

    def create(self, spec: SandboxSpec) -> None:
        # 镜像由安装器准备；这里不拉取、不启动常驻容器。
        _ = spec

    def destroy(self, spec: SandboxSpec) -> None:
        _ = spec

    def build_argv(
        self,
        spec: SandboxSpec,
        argv: Sequence[str],
        *,
        cwd: str = JAIL_WORKSPACE,
        env: dict[str, str] | None = None,
        read_paths: Sequence = (),
        write_paths: Sequence = (),
    ) -> list[str]:
        _ = (read_paths, write_paths)
        out = [
            self._runtime,
            "run",
            "--rm",
            "--interactive",
            "--network",
            "bridge" if spec.network else "none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            str(self._pids),
            "--memory",
            self._memory,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=256m",
            "-v",
            f"{_posix(spec.workspace_root)}:{JAIL_WORKSPACE}:rw",
            "-v",
            f"{_posix(spec.shared_inbox)}:{JAIL_INBOX}:ro",
            "-v",
            f"{_posix(spec.shared_outbox)}:{JAIL_OUTBOX}:rw",
            "-w",
            str(cwd or JAIL_WORKSPACE),
        ]
        for key, value in dict(env or {}).items():
            normalized_key = str(key or "").strip()
            if not normalized_key or normalized_key.startswith("G3KU_"):
                continue
            out.extend(["-e", f"{normalized_key}={value}"])
        out.append(self._image)
        out.extend(str(item) for item in argv)
        return out


def _posix(path: Any) -> str:
    return str(path).replace("\\", "/")


__all__ = ["ContainerBackend"]
