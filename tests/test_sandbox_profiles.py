"""jail profile 生成：最小根、禁绑路径、环境剥离。

这些断言是 P1a 的安全面：profile 写松一条 bind，宿主就进了 jail。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import main  # noqa: F401  (production import order)
from g3ku.sandbox.backend_base import SandboxSpec
from g3ku.sandbox.profiles import (
    JAIL_INBOX,
    JAIL_OUTBOX,
    JAIL_WORKSPACE,
    assert_profile_safe,
    build_bwrap_argv,
    build_sbpl_profile,
)


def _spec(tmp_path: Path, *, network: bool = False, read_roots=(), write_roots=()) -> SandboxSpec:
    workspace = tmp_path / "sandboxes" / "s1" / "workspace"
    inbox = tmp_path / "sandboxes" / "s1" / "shared" / "inbox"
    outbox = tmp_path / "sandboxes" / "s1" / "shared" / "outbox"
    for path in (workspace, inbox, outbox):
        path.mkdir(parents=True, exist_ok=True)
    return SandboxSpec(
        sandbox_id="s1",
        session_key="ext:qq-official-1:abc",
        workspace_root=workspace,
        shared_inbox=inbox,
        shared_outbox=outbox,
        read_roots=tuple(read_roots),
        write_roots=tuple(write_roots),
        network=network,
        trust="third_party_group",
    )


def test_namespaces_and_minimal_root(tmp_path: Path) -> None:
    argv = build_bwrap_argv(_spec(tmp_path), ["bash", "-lc", "pwd"])
    assert argv[0] == "bwrap"
    assert "--unshare-all" in argv
    assert "--die-with-parent" in argv
    assert "--new-session" in argv
    assert "--clearenv" in argv
    # 全新 /proc 与最小 /dev 是挡 WSL interop 与宿主设备的关键两条
    joined = " ".join(argv)
    assert "--proc /proc" in joined
    assert "--dev /dev" in joined
    assert "--tmpfs /tmp" in joined
    # 会话卷是唯一可写主根
    assert argv[argv.index("--chdir") + 1] == JAIL_WORKSPACE
    assert argv[-3:] == ["bash", "-lc", "pwd"]


def test_network_is_off_by_default_and_opt_in_is_explicit(tmp_path: Path) -> None:
    off = build_bwrap_argv(_spec(tmp_path), ["true"])
    assert "--share-net" not in off
    on = build_bwrap_argv(_spec(tmp_path, network=True), ["true"])
    assert "--share-net" in on


def test_environment_is_stripped(tmp_path: Path) -> None:
    argv = build_bwrap_argv(
        _spec(tmp_path),
        ["env"],
        env={"G3KU_API_KEY": "secret", "G3KU_TEST": "x", "NORMAL": "ok"},
    )
    joined = " ".join(argv)
    assert "G3KU_API_KEY" not in joined
    assert "G3KU_TEST" not in joined
    assert "--setenv NORMAL ok" in joined
    # 基础三元组由 profile 给
    assert "--setenv PATH /usr/local/bin:/usr/bin:/bin" in joined
    assert f"--setenv HOME {JAIL_WORKSPACE}" in joined


def test_forbidden_host_paths_are_never_bound(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        read_roots=[Path("/mnt/c/Users"), Path("/init"), Path("/root/.ssh")],
        write_roots=[Path("/mnt/d/out")],
    )
    argv = build_bwrap_argv(spec, ["true"])
    for index, token in enumerate(argv[:-1]):
        if token in {"--bind", "--ro-bind"}:
            target = argv[index + 1]
            assert not target.startswith("/mnt"), target
            assert not target.startswith("/init"), target
            assert not target.startswith("/root"), target


def test_workspace_under_forbidden_prefix_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec(tmp_path)
    bad = SandboxSpec(
        sandbox_id=spec.sandbox_id,
        session_key=spec.session_key,
        workspace_root=Path("/mnt/c/session"),
        shared_inbox=spec.shared_inbox,
        shared_outbox=spec.shared_outbox,
    )
    with pytest.raises(ValueError):
        build_bwrap_argv(bad, ["true"])


def test_empty_argv_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_bwrap_argv(_spec(tmp_path), [])


def test_forbidden_prefix_predicate() -> None:
    from g3ku.sandbox.profiles import _forbidden

    for path in (
        "/mnt/c/Users",
        "/mnt",
        "/init",
        "/root/.ssh/id_rsa",
        "/run/WSL/1",
        "/var/run/docker.sock",
        "/proc/sys/fs/binfmt_misc/WSLInterop",
    ):
        assert _forbidden(path), path
    for path in ("/usr/local/lib", "/workspace", "/home/negi", "/mntfoo"):
        assert not _forbidden(path), path


def test_assert_profile_safe_catches_injected_bind() -> None:
    with pytest.raises(ValueError):
        assert_profile_safe(["bwrap", "--ro-bind", "/mnt/c", "/mnt/c", "--", "true"])
    assert_profile_safe(["bwrap", "--ro-bind", "/usr", "/usr", "--", "true"])


def test_sbpl_profile_is_default_deny(tmp_path: Path) -> None:
    profile = build_sbpl_profile(_spec(tmp_path))
    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert str(_spec(tmp_path).workspace_root.as_posix()) in profile
    allowed = build_sbpl_profile(_spec(tmp_path, network=True))
    assert "(deny network*)" not in allowed


def test_inbox_and_outbox_are_the_only_shared_roots(tmp_path: Path) -> None:
    argv = build_bwrap_argv(_spec(tmp_path), ["true"])
    binds = {
        argv[index + 2]
        for index, token in enumerate(argv[:-2])
        if token in {"--bind", "--ro-bind"}
    }
    assert JAIL_INBOX in binds
    assert JAIL_OUTBOX in binds
    assert "/shared" not in binds


def _ro_targets(argv: list[str]) -> set[str]:
    return {
        argv[index + 2]
        for index, token in enumerate(argv[:-2])
        if token in {"--bind", "--ro-bind"}
    }


def test_system_roots_follow_the_backend_resolver(tmp_path: Path) -> None:
    """存在性判据属于 jail 那一侧，不是宿主。

    WSL 后端的宿主是 Windows：默认的宿主 stat 让 `Path("/usr")` 恒为 False，profile
    于是产出一个只绑了会话卷的空根，bwrap 连 `sh` 都 exec 不到。绑定必须跟着后端给的
    resolver 走。
    """
    distro = {"/usr", "/etc/localtime"}
    argv = build_bwrap_argv(
        _spec(tmp_path),
        ["sh", "-lc", "pwd"],
        path_exists=lambda path: path in distro,
        path_is_file=lambda path: path in distro,
    )
    assert "/usr" in _ro_targets(argv)
    assert "/etc/ssl" not in _ro_targets(argv)


def test_merged_usr_symlinks_are_decided_by_the_backend(tmp_path: Path) -> None:
    distro = {"/usr", "/usr/bin", "/usr/lib", "/usr/lib64"}
    argv = build_bwrap_argv(
        _spec(tmp_path),
        ["sh", "-lc", "pwd"],
        path_exists=lambda path: path in distro,
        path_is_file=lambda path: False,
    )
    symlinks = {tuple(argv[index + 1 : index + 3]) for index, token in enumerate(argv[:-2]) if token == "--symlink"}
    assert ("usr/bin", "/bin") in symlinks
    assert ("usr/lib", "/lib") in symlinks


def test_helper_bind_uses_the_backend_is_file(tmp_path: Path) -> None:
    spec = replace(
        _spec(tmp_path),
        helper_binds=((Path("/home/negi/.negi-sandbox/fsop.py"), "/opt/negi/fsop.py"),),
    )
    bound = build_bwrap_argv(
        spec,
        ["true"],
        path_exists=lambda path: path == "/usr",
        path_is_file=lambda path: path.endswith("fsop.py"),
    )
    assert "/opt/negi/fsop.py" in _ro_targets(bound)
    missing = build_bwrap_argv(
        spec,
        ["true"],
        path_exists=lambda path: path == "/usr",
        path_is_file=lambda path: False,
    )
    assert "/opt/negi/fsop.py" not in _ro_targets(missing)
