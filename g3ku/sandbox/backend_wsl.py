"""Windows 后端：复用发行版里的 bwrap。

``wsl.exe -d <distro> -- bwrap …``：发行版只提供 userland 与内核 namespace 能力，
隔离仍由 bwrap 的 profile 提供。发行版可以是用户自己的（探测按 bwrap + python3 打分），
也可以是 ``negi-sandbox``。``/mnt`` 与 ``/init`` 从不 bind，所以发行版的 automount
不构成泄露；``--unshare-pid`` + 全新 ``/proc`` 把 WSL interop 的 binfmt 挡在 jail 外。
"""

from __future__ import annotations

import subprocess
from typing import Any, Sequence

from g3ku.sandbox.backend_base import Enforcement
from g3ku.sandbox.backend_bwrap import BwrapBackend
from g3ku.sandbox.errors import SandboxUnavailableError
from g3ku.sandbox.probe import FULL, PARTIAL
from g3ku.sandbox.profiles import (
    JAIL_WORKSPACE,
    RO_SYSTEM_ROOTS,
    assert_profile_safe,
    build_bwrap_argv,
)

# profile 一族要判存在性的固定候选：一次问完发行版，别按条各起一个 wsl 进程。
_STATIC_PATH_CANDIDATES = tuple(
    dict.fromkeys(RO_SYSTEM_ROOTS + ("/etc/resolv.conf", "/usr/bin", "/usr/lib", "/usr/lib64"))
)

_DISTRO_TIMEOUT_SECONDS = 30


class _DistroPaths:
    """发行版内的路径存在性快照。

    宿主是 Windows 时，`Path("/usr").exists()` 恒为 False，profile 就一条 bind 都不发，
    jail 起来是个空根（`bwrap: execvp sh: No such file or directory`）。存在性必须由
    发行版本人回答。
    """

    def __init__(self, wsl_path: str, distro: str) -> None:
        self._wsl = str(wsl_path)
        self._distro = str(distro)
        self._exists: dict[str, bool] = {}
        self._files: dict[str, bool] = {}
        self.readable = False
        self._seed()

    def _run(self, argv: Sequence[str]) -> tuple[int, str]:
        try:
            completed = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
                [self._wsl, "-d", self._distro, "-e", *argv],
                capture_output=True,
                timeout=_DISTRO_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return -1, ""
        return int(completed.returncode), (completed.stdout or b"").decode("utf-8", "replace")

    def _seed(self) -> None:
        # 候选集是模块常量，拼进脚本不涉及外部输入；动态路径走 _check 的位置参数。
        listing = " ".join(f"'{path}'" for path in _STATIC_PATH_CANDIDATES)
        script = (
            f"for p in {listing}; do "
            'if [ -e "$p" ]; then if [ -f "$p" ]; then echo "F $p"; '
            'else echo "E $p"; fi; fi; done'
        )
        code, out = self._run(["sh", "-c", script])
        if code != 0:
            return
        self.readable = True
        for line in out.splitlines():
            kind, _, path = line.strip().partition(" ")
            if not path:
                continue
            self._exists[path] = True
            if kind == "F":
                self._files[path] = True

    def _check(self, flag: str, path: str) -> bool:
        cache = self._files if flag == "f" else self._exists
        if path in cache:
            return cache[path]
        if path in self._exists or path in self._files:
            # 种子已确认它不在发行版里，不必再问一次。
            cache[path] = False
            return False
        # 路径可能来自配置，只能当位置参数递进去，不能拼进脚本。
        code, out = self._run(["sh", "-c", f'test -{flag} "$1" && echo yes', "sh", path])
        found = code == 0 and "yes" in out
        cache[path] = found
        return found

    def exists(self, path: str) -> bool:
        return self._check("e", str(path or ""))

    def is_file(self, path: str) -> bool:
        return self._check("f", str(path or ""))


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
        self._paths = _DistroPaths(self._wsl, self._distro)

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
        if not self._paths.readable:
            # 问不到发行版就宁可不执行：空根 jail 看着像"绑上了"，实际什么都不是。
            raise SandboxUnavailableError(f"wsl distro {self._distro} is not inspectable")
        inner = build_bwrap_argv(
            spec,
            argv,
            cwd=cwd,
            env=env,
            bwrap_path=self._bwrap,
            path_exists=self._paths.exists,
            path_is_file=self._paths.is_file,
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
