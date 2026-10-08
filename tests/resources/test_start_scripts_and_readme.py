import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_start_negi_powershell_help_outputs_usage_for_short_and_long_flags() -> None:
    script = REPO_ROOT / "start-negi.ps1"

    short = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-h",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    long = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "--help",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert short.returncode == 0
    assert long.returncode == 0
    assert "Usage:" in short.stdout
    assert "Usage:" in long.stdout
    assert "start-negi.ps1" in short.stdout
    assert "-BindHost" in short.stdout
    assert "-Reload" in short.stdout
    assert "-OpenBrowser" in short.stdout


def test_start_negi_shell_script_has_richer_help_text() -> None:
    shell_text = _read("start-negi.sh")

    assert "-h|--help)" in shell_text
    assert "Usage: ./start-negi.sh" in shell_text
    assert "Common options:" in shell_text
    assert "--open-browser" in shell_text
    assert "--reload" in shell_text


def test_wrappers_exec_the_bootstrapper_by_its_current_name() -> None:
    # 入口是三层链：start-negi.* → negi.* → negi_bootstrap.py。任何一环按旧名 exec，
    # 双击启动就会指向一个升级后不存在的文件。
    assert '"negi.ps1"' in _read("start-negi.ps1")
    assert "/negi.sh" in _read("start-negi.sh")
    assert "start-negi.ps1" in _read("start-negi.cmd")
    for wrapper in ("negi.ps1", "negi.sh", "negi.cmd"):
        assert "negi_bootstrap.py" in _read(wrapper), wrapper


def test_stale_process_reaper_keeps_both_cmdline_shapes() -> None:
    # 入口名换成 negi，进程形状没换：托管 worker、升级后重拉起与容器入口都用 `-m g3ku`，
    # 只有走包装启动的那一跳才是 negi_bootstrap.py。判据少一条就会留下占着端口的实例。
    ps_text = _read("start-negi.ps1")
    sh_text = _read("start-negi.sh")
    assert r"-m\s+g3ku\s+web" in ps_text
    assert r"-m\s+g3ku\s+worker" in ps_text
    assert r"negi_bootstrap\.py" in ps_text
    assert "/-m[[:space:]]+g3ku[[:space:]]+web/" in sh_text
    assert "/-m[[:space:]]+g3ku[[:space:]]+worker/" in sh_text
    assert r"negi_bootstrap\.py" in sh_text


def test_readme_launches_via_start_script_and_keeps_typed_commands_later() -> None:
    readme = _read("README.md")

    startup_section = readme[readme.index("## 2. 快速开始"):readme.index("## 3. Web 界面")]
    manual_section = readme[readme.index("## 11. 手动安装与命令行"):]

    assert "start-negi" in startup_section
    assert "negi web" not in startup_section
    assert "negi worker" not in startup_section
    assert "negi web" in manual_section
    assert "negi worker" in manual_section
