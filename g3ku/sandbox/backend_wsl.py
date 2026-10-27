"""Windows 后端：复用发行版里的 bwrap。

``wsl.exe -d <distro> -- bwrap …``：发行版只提供 userland 与内核 namespace 能力，
隔离仍由 bwrap 的 profile 提供。发行版可以是用户自己的（探测按 bwrap + python3 打分），
也可以是 ``negi-sandbox``。``/mnt`` 与 ``/init`` 从不 bind，所以发行版的 automount
不构成泄露；``--unshare-pid`` + 全新 ``/proc`` 把 WSL interop 的 binfmt 挡在 jail 外。
"""

from __future__ import annotations

from typing import Any, Sequence

from g3ku.sandbox.backend_base import Enforcement
from g3ku.sandbox.backend_bwrap import BwrapBackend
from g3ku.sandbox.probe import FULL, PARTIAL
from g3ku.sandbox.profiles import JAIL_WORKSPACE, assert_profile_safe, build_bwrap_argv


class WslBwrapBackend(BwrapBackend):
    name = "wsl_bwrap"
    # 外层是 wsl.exe：宿主环境（SystemRoot、WSLENV…）必须原样传，否则它起不来。
    # 进 jail 的环境由 bwrap 的 --clearenv/--setenv 决定，两者互不影响。
    keeps_host_env = True

    def __init__(
        self,
        *,
        wsl_path: str,
        distro: str,
        bwrap_path: str = "bwrap",
        enforcement: Enforcement | None = None,
    ):
        super().__init__(bwrap_path=bwrap_path, enforcement=enforcement)
        self._wsl = str(wsl_path)
        self._distro = str(distro)

    @property
    def distro(self) -> str:
        return self._distro

    def build_argv(
        self,
        spec,
        argv: Sequence[str],
        *,
        cwd: str = JAIL_WORKSPACE,
        env: dict[str, str] | None = None,
        read_paths: Sequence = (),
        write_paths: Sequence = (),
    ) -> list[str]:
        inner = build_bwrap_argv(
            spec,
            argv,
            cwd=cwd,
            env=env,
            bwrap_path=self._bwrap,
        )
        assert_profile_safe(inner)
        return [self._wsl, "-d", self._distro, "--", *inner]

    def probe(self) -> dict[str, Any]:
        return {
            "state": "ready",
            "backend": self.name,
            "enforcement": self.enforcement().as_dict(),
            "detail": {"distro": self._distro},
        }


__all__ = ["WslBwrapBackend", "FULL", "PARTIAL"]
