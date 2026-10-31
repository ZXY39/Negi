import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

BOM = b"\xef\xbb\xbf"


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
    # 双击启动就会指向一个升级后不存在的文件。`.cmd` 是机器脚本策略无关的那条道，
    # 直接 exec `negi_bootstrap.py`（不经 PowerShell）。
    assert '"negi.ps1"' in _read("start-negi.ps1")
    assert "/negi.sh" in _read("start-negi.sh")
    cmd_text = _read("start-negi.cmd")
    assert "negi_bootstrap.py" in cmd_text
    assert "-executionpolicy" not in cmd_text.lower()
    assert "-file" not in cmd_text.lower()
    for wrapper in ("negi.ps1", "negi.sh", "negi.cmd"):
        assert "negi_bootstrap.py" in _read(wrapper), wrapper


def test_stale_process_reaper_keeps_both_cmdline_shapes() -> None:
    # 入口名换成 negi，进程形状没换：托管 worker、升级后重拉起与容器入口都用 `-m g3ku`，
    # 只有走包装启动的那一跳才是 negi_bootstrap.py。判据少一条就会留下占着端口的实例。
    # Windows 侧的回收在解释器里（`.ps1` 会被脚本策略整份拒掉，回收不能挂在那儿）。
    boot_text = _read("negi_bootstrap.py")
    sh_text = _read("start-negi.sh")
    assert r"-m\s+g3ku\s+web" in boot_text
    assert r"-m\s+g3ku\s+worker" in boot_text
    assert r"negi_bootstrap\.py" in boot_text
    assert "/-m[[:space:]]+g3ku[[:space:]]+web/" in sh_text
    assert "/-m[[:space:]]+g3ku[[:space:]]+worker/" in sh_text
    assert r"negi_bootstrap\.py" in sh_text


def test_readme_launches_via_start_script_and_keeps_typed_commands_later() -> None:
    readme = _read("README.md")

    # 日常启动段只给"不需要知道细节"的入口；`negi web` / `negi worker` 是前台与
    # 拆环节排查用的，留在后面的命令一览里，否则用户会照着前台命令起服务。
    startup_section = readme[readme.index("### 启动和停止"):readme.index("## 群聊里的它")]
    manual_section = readme[readme.index("### 命令一览"):]

    assert "start-negi" in startup_section
    assert "negi web" not in startup_section
    assert "negi worker" not in startup_section
    assert "negi web" in manual_section
    assert "negi worker" in manual_section


def test_readme_documents_daily_start_and_uninstall() -> None:
    readme = _read("README.md")

    assert "### 启动和停止" in readme
    assert "项目口令" in readme
    assert "### 卸载" in readme
    uninstall_section = readme[readme.index("### 卸载"):readme.index("### 安装脚本参数")]
    for phrase in ("已安装的应用", "uninstall.ps1", "uninstall.sh", "保留数据", "全部删除"):
        assert phrase in uninstall_section, phrase


def test_installer_is_bom_free_so_the_one_line_channel_parses() -> None:
    # `iwr .../install.ps1 | iex` gives PowerShell the downloaded bytes decoded as
    # text, so a UTF-8 BOM arrives as junk characters *before* `#requires`. That
    # makes `param(` no longer the first statement and the one-line install dies
    # with InvalidLeftHandSide on its first parameter line.
    for name in ("install.ps1", "install.sh", "uninstall.sh"):
        assert not (REPO_ROOT / name).read_bytes().startswith(BOM), name


def test_installer_is_pure_ascii() -> None:
    # Without a BOM, PowerShell 5.1 reads a script file in the machine's ANSI code
    # page. A Chinese comment then decodes into bytes that can end with a backtick,
    # which is a line continuation: the comment silently swallows the next
    # statement. That is how Install-CodeInto lost its `$stageRoot` assignment and
    # a fresh install died with "variable is not defined" - and it parses clean, so
    # only running it (or keeping the file ASCII) catches it.
    lines = (REPO_ROOT / "install.ps1").read_bytes().split(b"\n")
    offenders = [
        (number, line.decode("utf-8", "replace")[:70])
        for number, line in enumerate(lines, start=1)
        if any(byte > 127 for byte in line)
    ]
    assert offenders == [], offenders


def test_uninstaller_stays_readable_through_the_file_channel() -> None:
    # uninstall.ps1 is reached only by path (`-File`, from the uninstall wizard or from
    # the installed-apps row), never through `iwr | iex`. PowerShell 5.1 decodes a
    # non-BOM script in the machine ANSI code page, which is fatal as soon as the file
    # carries non-ASCII text. The file is ASCII today and keeps its BOM as the safety
    # net; the check below is what fails loudly if either half drifts.
    raw = (REPO_ROOT / "uninstall.ps1").read_bytes()
    assert raw.startswith(BOM)
    body = raw[3:]
    offenders = [
        (number, line.decode("utf-8", "replace")[:70])
        for number, line in enumerate(body.split(b"\n"), start=1)
        if any(byte > 127 for byte in line)
    ]
    assert offenders == [], offenders


def test_every_shipped_powershell_script_is_ascii_or_bom_marked() -> None:
    # The two rules above generalize: a script that is executed by path and carries
    # non-ASCII text needs the BOM, and the one the `iwr | iex` channel pipes needs
    # to stay pure ASCII. packaging\setup-run.ps1 is the wizard's hand-off into
    # install.ps1 and prints Chinese, so it is exactly as exposed as uninstall.ps1
    # was - which is why the check walks the folder instead of naming files.
    scripts = list(REPO_ROOT.glob("*.ps1")) + list((REPO_ROOT / "packaging").glob("*.ps1"))
    offenders = []
    for path in sorted(scripts):
        raw = path.read_bytes()
        if any(byte > 127 for byte in raw) and not raw.startswith(BOM):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], offenders
