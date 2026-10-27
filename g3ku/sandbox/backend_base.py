"""沙箱后端契约。

一个后端只需要回答四件事：怎么探测、怎么把一次调用包成 argv、怎么建/拆会话卷、
拿得到哪几维强制力。**没有常驻进程、没有 RPC、没有监听端口**——每次调用都是一个
一次性子进程（D16）。

``SandboxSpec`` 是执行侧唯一需要知道的沙箱事实：根在哪、能写哪、能不能联网。
它由 ``manager`` 从会话键与档位算出来，后端不做策略判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from g3ku.sandbox.probe import FULL, MISSING, PARTIAL


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    sandbox_id: str
    session_key: str
    workspace_root: Path
    shared_inbox: Path
    shared_outbox: Path
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()
    network: bool = False
    trust: str = ""
    kernel_name: str = "Linux"
    # 只读助手文件（宿主路径 → jail 内路径）。`fsop` 靠它进 jail：jail 里没有仓库，
    # 绑一个 stdlib-only 的单文件就能跑，不需要装包。
    helper_binds: tuple[tuple[Path, str], ...] = ()

    def exists(self) -> bool:
        return self.workspace_root.exists()


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    kind: str = ""
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class Enforcement:
    fs_read: str = MISSING
    fs_write: str = MISSING
    network: str = MISSING
    process: str = MISSING
    resource: str = MISSING

    def as_dict(self) -> dict[str, str]:
        return {
            "fs_read": self.fs_read,
            "fs_write": self.fs_write,
            "network": self.network,
            "process": self.process,
            "resource": self.resource,
        }

    def satisfies_required(self) -> bool:
        """第三方会话的必需维度：读、写、网络，三者都必须 full。"""
        return all(
            dimension == FULL
            for dimension in (self.fs_read, self.fs_write, self.network)
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "Enforcement":
        source = dict(payload or {})
        return cls(
            fs_read=str(source.get("fs_read") or MISSING),
            fs_write=str(source.get("fs_write") or MISSING),
            network=str(source.get("network") or MISSING),
            process=str(source.get("process") or MISSING),
            resource=str(source.get("resource") or MISSING),
        )


@runtime_checkable
class SandboxBackend(Protocol):
    name: str

    def probe(self) -> dict[str, Any]:
        """返回 ``{state, reason, enforcement, detail}``（与 `probe.probe_install_state` 同形）。"""

    def enforcement(self) -> Enforcement:
        ...

    def create(self, spec: SandboxSpec) -> None:
        """准备会话卷（目录）。幂等。"""

    def destroy(self, spec: SandboxSpec) -> None:
        """拆会话卷。卷里有用户数据时由调用方先导出。"""

    def build_argv(
        self,
        spec: SandboxSpec,
        argv: Sequence[str],
        *,
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
        read_paths: Sequence[Path] = (),
        write_paths: Sequence[Path] = (),
    ) -> list[str]:
        """把一次调用包成后端可执行的 argv。"""


@dataclass(slots=True)
class BackendSelection:
    """一次探测的结论：选中的后端 + 逐维度强制力 + 为什么。"""

    backend: SandboxBackend | None = None
    name: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    enforcement: Enforcement = field(default_factory=Enforcement)
    third_party_usable: bool = False


__all__ = [
    "BackendSelection",
    "Enforcement",
    "ExecResult",
    "SandboxBackend",
    "SandboxSpec",
    "FULL",
    "MISSING",
    "PARTIAL",
]
