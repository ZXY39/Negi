"""WSL 后端的调用形态：argv 不能在进 bwrap 之前先过一遍 guest 的 shell。

`wsl.exe -- <argv>` 把剩余参数拼成一条命令交给发行版的默认 shell，于是模型命令里的
`$( )` 与 `$HOME` 在 **jail 外**求值：`ls /` 报出发行版整根、`ls /mnt` 数出宿主盘符、
`$HOME` 是 `/home/<user>` 而不是 `/workspace`。验收清单第 3 条与第 10 条会因此假通过。
`-e` 是 exec 形态，不做这层解析。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import main  # noqa: F401  (production import order)
from g3ku.sandbox import backend_wsl
from g3ku.sandbox.backend_base import SandboxSpec
from g3ku.sandbox.backend_wsl import WslBwrapBackend
from g3ku.sandbox.errors import SandboxUnavailableError


class _Completed:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout.encode("utf-8")
        self.stderr = b""


def _spec(tmp_path: Path) -> SandboxSpec:
    volume = tmp_path / "sandboxes" / "s1"
    for name in ("workspace", "shared/inbox", "shared/outbox"):
        (volume / name).mkdir(parents=True, exist_ok=True)
    return SandboxSpec(
        sandbox_id="s1",
        session_key="web:ceo-1",
        workspace_root=volume / "workspace",
        shared_inbox=volume / "shared" / "inbox",
        shared_outbox=volume / "shared" / "outbox",
        trust="owner",
    )


_DISTRO_LISTING = "\n".join(
    [
        "E /usr",
        "E /bin",
        "E /lib",
        "E /lib64",
        "E /etc/localtime",
        "F /etc/resolv.conf",
    ]
)


def _backend(monkeypatch: pytest.MonkeyPatch, result: _Completed) -> WslBwrapBackend:
    monkeypatch.setattr(backend_wsl.subprocess, "run", lambda *args, **kwargs: result)
    return WslBwrapBackend(wsl_path="wsl.exe", distro="negi-sandbox")


def test_wsl_argv_uses_the_exec_form_not_the_shell_form(tmp_path: Path, monkeypatch) -> None:
    backend = _backend(monkeypatch, _Completed(0, _DISTRO_LISTING))
    argv = backend.build_argv(_spec(tmp_path), ["sh", "-lc", "pwd"])
    assert argv[:4] == ["wsl.exe", "-d", "negi-sandbox", "-e"]
    # bwrap 自己的 `--` 还在，它隔开的是 profile 与 jail 内命令，与 wsl.exe 无关。
    assert argv.count("--") >= 1


def test_wsl_argv_binds_the_roots_the_distro_reports(tmp_path: Path, monkeypatch) -> None:
    backend = _backend(monkeypatch, _Completed(0, _DISTRO_LISTING))
    argv = backend.build_argv(_spec(tmp_path), ["sh", "-lc", "pwd"])
    ro = {argv[index + 2] for index, token in enumerate(argv[:-2]) if token == "--ro-bind"}
    assert {"/usr", "/bin"} <= ro
    assert "/etc/ssl" not in ro  # 发行版没报的路径不绑
    assert "--symlink" not in argv  # /bin 真存在时不需要 merged-usr 补链


def test_wsl_argv_refuses_to_build_when_the_distro_is_uninspectable(
    tmp_path: Path, monkeypatch
) -> None:
    backend = _backend(monkeypatch, _Completed(1, ""))
    with pytest.raises(SandboxUnavailableError):
        backend.build_argv(_spec(tmp_path), ["sh", "-lc", "pwd"])
