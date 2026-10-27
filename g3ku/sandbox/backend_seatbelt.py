"""macOS 后端：Seatbelt（``sandbox-exec``）。

profile 由 ``profiles.build_sbpl_profile`` 生成：默认全拒、按最小根放行、无网络。
环境靠调用方传入的净化环境（Seatbelt 本身不清环境），``keeps_host_env = False``。
``sandbox-exec`` 被 Apple 标记 deprecated，按"能用但不演进"对待，容器后端是备选。
"""

from __future__ import annotations

from typing import Any, Sequence

from g3ku.sandbox.backend_base import Enforcement, SandboxSpec
from g3ku.sandbox.probe import FULL, MISSING
from g3ku.sandbox.profiles import JAIL_WORKSPACE, build_sbpl_profile


class SeatbeltBackend:
    name = "seatbelt"
    keeps_host_env = False

    def __init__(self, *, sandbox_exec_path: str = "sandbox-exec", enforcement: Enforcement | None = None):
        self._exe = str(sandbox_exec_path or "sandbox-exec")
        self._enforcement = enforcement or Enforcement(
            fs_read=FULL,
            fs_write=FULL,
            network=FULL,
            process=MISSING,
            resource=MISSING,
        )

    def probe(self) -> dict[str, Any]:
        return {
            "state": "ready",
            "backend": self.name,
            "enforcement": self.enforcement().as_dict(),
            "detail": {"binary": self._exe, "deprecated": True},
        }

    def enforcement(self) -> Enforcement:
        return self._enforcement

    def create(self, spec: SandboxSpec) -> None:
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
        _ = (cwd, env, read_paths, write_paths)
        profile = build_sbpl_profile(spec)
        return [self._exe, "-p", profile, "--", *[str(item) for item in argv]]


__all__ = ["SeatbeltBackend"]
