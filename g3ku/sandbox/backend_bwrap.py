"""Linux 后端：bubblewrap。

只做三件事：拿 profile 拼 argv、执行前跑一次禁绑断言、报告强制力。
**没有常驻进程**：每次调用一个 bwrap 子进程，namespace 随进程退出消失。
"""

from __future__ import annotations

from typing import Any, Sequence

from g3ku.sandbox.backend_base import Enforcement, SandboxSpec
from g3ku.sandbox.probe import FULL, PARTIAL, probe_install_state
from g3ku.sandbox.profiles import (
    JAIL_WORKSPACE,
    assert_profile_safe,
    build_bwrap_argv,
)


class BwrapBackend:
    name = "bwrap"
    # bwrap 的 --clearenv 会清空环境，宿主环境不参与；但 wsl.exe 需要宿主的
    # SystemRoot 等变量才能启动，所以这个标志只对真·本地后端生效。
    keeps_host_env = False

    def __init__(self, *, bwrap_path: str = "bwrap", enforcement: Enforcement | None = None):
        self._bwrap = str(bwrap_path or "bwrap")
        self._enforcement = enforcement or Enforcement(
            fs_read=FULL,
            fs_write=FULL,
            network=FULL,
            process=FULL,
            resource=PARTIAL,
        )

    def probe(self) -> dict[str, Any]:
        return probe_install_state()

    def enforcement(self) -> Enforcement:
        return self._enforcement

    def create(self, spec: SandboxSpec) -> None:
        # 目录由 manager 的布局层准备；bwrap 没有可创建的"实例"。
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
        wrapped = build_bwrap_argv(
            spec,
            argv,
            cwd=cwd,
            env=env,
            bwrap_path=self._bwrap,
        )
        # profile 生成时判过一遍，执行前再判一遍：中间任何人插一条 bind 都要被拦下。
        assert_profile_safe(wrapped)
        return wrapped


__all__ = ["BwrapBackend"]
