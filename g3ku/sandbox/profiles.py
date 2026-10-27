"""jail 的 argv / 沙箱配置文件生成——**安全关键代码的唯一产地**。

两条纪律：

1. profile 只在这里生成。后端文件只做"调用它 + 执行"，不各自拼 bind。
2. **白名单式最小根**：jail 里只有 `/workspace`、`/shared`、只读系统根、`/tmp`、`/proc`、`/dev`。
   宿主 workspace、`.g3ku`、`memory/`、`sessions/`、`$HOME`、`/mnt`（WSL 的 Windows 盘）、
   `/init`（WSL interop 入口）、容器运行时 socket 一律不进。

`/mnt` 与 `/init` 这条对 WSL 尤其重要：发行版的 automount 开着时它们确实存在，但 jail 的
mount namespace 是我们自己建的，不 bind 就看不见；`--unshare-pid` + 全新 `/proc` 又把
binfmt 里的 WSLInterop 一起挡在外面（否则 jail 里能直接跑 `.exe`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from g3ku.sandbox.backend_base import SandboxSpec

# 只读系统根：存在才 bind（不同发行版 / macOS 的布局不同）。
RO_SYSTEM_ROOTS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/lib",
    "/lib64",
    "/lib32",
    "/sbin",
    "/etc/ssl",
    "/etc/alternatives",
    "/etc/ld.so.cache",
    "/etc/localtime",
    "/opt/homebrew",
)

# 绝不 bind 的宿主路径前缀：命中即视为 profile 写错。
FORBIDDEN_BIND_PREFIXES: tuple[str, ...] = (
    "/mnt",
    "/init",
    "/root",
    "/run/WSL",
    "/run/user",
    "/var/run/docker.sock",
    "/var/run/podman",
    "/proc/sys/fs/binfmt_misc",
)

JAIL_WORKSPACE = "/workspace"
JAIL_INBOX = "/shared/inbox"
JAIL_OUTBOX = "/shared/outbox"
JAIL_TMP = "/tmp"


def _as_posix(path: Path | str) -> str:
    return Path(path).as_posix()


def _forbidden(path: str) -> bool:
    normalized = _as_posix(path)
    return any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in FORBIDDEN_BIND_PREFIXES
    )


def _existing_ro_roots() -> list[str]:
    roots: list[str] = []
    for candidate in RO_SYSTEM_ROOTS:
        try:
            if Path(candidate).exists():
                roots.append(candidate)
        except OSError:
            continue
    return roots


class _MountPlan:
    """收集 bind 目标路径并按需补父目录。

    bwrap 会创建 bind 的**目标本身**，但目标的父目录要先存在；`/etc/ssl` 这类两级目标
    在空根里没有 `/etc`，直接 bind 会在启动阶段失败。父目录一律建成空目录——它们只用于
    承载后面的 bind，不泄露宿主内容。
    """

    def __init__(self) -> None:
        self.args: list[str] = []
        self._dirs: set[str] = {"/", "/workspace", "/shared", "/shared/inbox", "/shared/outbox", "/tmp", "/proc", "/dev", "/home"}

    def _ensure_parents(self, dest: str) -> None:
        parts = [part for part in dest.split("/") if part]
        for index in range(1, len(parts)):
            parent = "/" + "/".join(parts[:index])
            if parent not in self._dirs:
                self.args.extend(["--dir", parent])
                self._dirs.add(parent)

    def bind(self, source: str, dest: str, *, writable: bool) -> None:
        self._ensure_parents(dest)
        self.args.extend(["--bind" if writable else "--ro-bind", source, dest])

    def dir(self, path: str) -> None:
        if path not in self._dirs:
            self.args.extend(["--dir", path])
            self._dirs.add(path)


def build_bwrap_argv(
    spec: SandboxSpec,
    argv: Sequence[str],
    *,
    cwd: str = JAIL_WORKSPACE,
    env: dict[str, str] | None = None,
    bwrap_path: str = "bwrap",
) -> list[str]:
    """把一次调用包成 bwrap argv。

    ``env`` 是**追加**的环境变量；基础三元组（PATH/HOME/TMPDIR/LANG/SANDBOX_ID）由这里给，
    宿主环境整体不继承（``--clearenv``），所以任何 ``G3KU_*`` 密钥都进不去。
    """
    if not list(argv or ()):
        raise ValueError("sandbox argv is empty")
    for candidate in (spec.workspace_root, spec.shared_inbox, spec.shared_outbox):
        if _forbidden(str(candidate)):
            raise ValueError(f"refusing to bind forbidden host path into the jail: {candidate}")

    out: list[str] = [
        bwrap_path,
        # net / pid / ipc / uts / cgroup 全拆；network 单独放行时再 --share-net。
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "HOME",
        JAIL_WORKSPACE,
        "--setenv",
        "TMPDIR",
        JAIL_TMP,
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "SANDBOX_ID",
        spec.sandbox_id,
    ]
    if spec.network:
        out.append("--share-net")

    plan = _MountPlan()
    for root in _existing_ro_roots():
        plan.bind(root, root, writable=False)
    # 常见发行版把 /bin、/lib 做成 /usr 下的软链；宿主上不存在时显式补一条。
    for link_target, link_name in (("usr/bin", "/bin"), ("usr/lib", "/lib"), ("usr/lib64", "/lib64")):
        if not Path(link_name).exists() and Path("/" + link_target).exists():
            plan.dir(link_name.rsplit("/", 1)[0] or "/")
            plan.args.extend(["--symlink", link_target, link_name])

    plan.args.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", JAIL_TMP])
    if spec.network and Path("/etc/resolv.conf").exists():
        plan.bind("/etc/resolv.conf", "/etc/resolv.conf", writable=False)

    # 会话卷：唯一可写的主根。
    plan.bind(_as_posix(spec.workspace_root), JAIL_WORKSPACE, writable=True)
    plan.bind(_as_posix(spec.shared_inbox), JAIL_INBOX, writable=True)
    plan.bind(_as_posix(spec.shared_outbox), JAIL_OUTBOX, writable=True)
    plan.dir("/home")

    # 额外读根（owner 档）：以同路径进 jail；命中禁绑前缀一律拒绝。
    for path in spec.read_roots:
        host = _as_posix(path)
        if _forbidden(host) or host in (_as_posix(spec.workspace_root),):
            continue
        if not Path(host).exists():
            continue
        plan.bind(host, host, writable=False)
    # 额外写根（owner 档的 additional_writable_roots）。
    for path in spec.write_roots:
        host = _as_posix(path)
        if _forbidden(host) or host in (_as_posix(spec.workspace_root),):
            continue
        if not Path(host).exists():
            continue
        plan.bind(host, host, writable=True)
    # 助手文件（fsop）：单文件只读绑进 jail，jail 里不需要装包。
    for source, dest in spec.helper_binds:
        if not Path(source).is_file():
            continue
        plan.bind(_as_posix(source), dest, writable=False)
    out.extend(plan.args)

    for key, value in dict(env or {}).items():
        normalized_key = str(key or "").strip()
        if not normalized_key or normalized_key.startswith("G3KU_"):
            continue
        out.extend(["--setenv", normalized_key, str(value)])

    out.extend(["--chdir", str(cwd or JAIL_WORKSPACE)])
    out.append("--")
    out.extend(str(item) for item in argv)
    return out


def build_sbpl_profile(spec: SandboxSpec) -> str:
    """macOS Seatbelt profile：默认全拒，再按最小根放行。

    与 bwrap 的差别：Seatbelt 是"先 deny default 再 allow"，所以要显式列出可读/可写的
    子路径；网络在**默认拒绝**下天然不可用，额外写 ``(deny network*)`` 只是把意图写明。
    """
    def _q(path: Path | str) -> str:
        return _as_posix(path).replace('"', '\\"')

    read_rules = [
        "(subpath \"/usr\")",
        "(subpath \"/bin\")",
        "(subpath \"/sbin\")",
        "(subpath \"/System/Library\")",
        "(subpath \"/Library/Apple\")",
        "(subpath \"/private/tmp\")",
        f"(subpath \"{_q(spec.workspace_root)}\")",
        f"(subpath \"{_q(spec.shared_inbox)}\")",
        f"(subpath \"{_q(spec.shared_outbox)}\")",
    ]
    for path in spec.read_roots:
        host = _as_posix(path)
        if _forbidden(host):
            continue
        read_rules.append(f"(subpath \"{_q(host)}\")")
    write_rules = [
        f"(subpath \"{_q(spec.workspace_root)}\")",
        f"(subpath \"{_q(spec.shared_outbox)}\")",
        "(subpath \"/private/tmp\")",
    ]
    for path in spec.write_roots:
        host = _as_posix(path)
        if _forbidden(host):
            continue
        write_rules.append(f"(subpath \"{_q(host)}\")")
    # Seatbelt 没有 mount namespace：助手文件（fsop）按宿主路径执行，必须显式放行只读。
    for source, _dest in spec.helper_binds:
        read_rules.append(f"(literal \"{_q(source)}\")")
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec* (subpath \"/usr/bin\") (subpath \"/bin\") (subpath \"/usr/local/bin\"))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow signal (target self))",
        "(allow file-read* " + " ".join(read_rules) + ")",
        "(allow file-write* " + " ".join(write_rules) + ")",
    ]
    if not spec.network:
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


def assert_profile_safe(argv: Sequence[str]) -> None:
    """兜底断言：生成好的 bwrap argv 里不许出现禁绑宿主路径。

    放在生成函数内部之外，供后端在真正执行前再跑一次——profile 是安全关键代码，
    "生成时判过"不足以阻止后续有人在中间插入一条 bind。
    """
    tokens = [str(item) for item in list(argv or ())]
    for index, token in enumerate(tokens[:-1]):
        if token in {"--bind", "--ro-bind", "--dev-bind", "--bind-try", "--ro-bind-try"}:
            target = tokens[index + 1]
            if _forbidden(target):
                raise ValueError(f"forbidden bind in sandbox argv: {target}")


__all__ = [
    "FORBIDDEN_BIND_PREFIXES",
    "JAIL_INBOX",
    "JAIL_OUTBOX",
    "JAIL_TMP",
    "JAIL_WORKSPACE",
    "RO_SYSTEM_ROOTS",
    "assert_profile_safe",
    "build_bwrap_argv",
    "build_sbpl_profile",
]
