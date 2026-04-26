import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_start_g3ku_powershell_help_outputs_usage_for_short_and_long_flags() -> None:
    script = REPO_ROOT / "start-g3ku.ps1"

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
    assert "start-g3ku.ps1" in short.stdout
    assert "-BindHost" in short.stdout
    assert "-Reload" in short.stdout
    assert "-OpenBrowser" in short.stdout


def test_start_g3ku_shell_script_has_richer_help_text() -> None:
    shell_text = (REPO_ROOT / "start-g3ku.sh").read_text(encoding="utf-8")

    assert "-h|--help)" in shell_text
    assert "Usage: ./start-g3ku.sh" in shell_text
    assert "Common options:" in shell_text
    assert "--open-browser" in shell_text
    assert "--reload" in shell_text


def test_readme_uses_start_script_as_primary_launch_and_moves_manual_commands_later() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    startup_section_index = readme.index("## 2. 如何启动项目")
    feature_section_index = readme.index("## 5. 功能介绍")
    developer_section_index = readme.index("## 6. 面向开发者和 Agent 的补充说明")

    startup_section = readme[startup_section_index:feature_section_index]
    developer_section = readme[developer_section_index:]

    assert ".\\start-g3ku.ps1" in startup_section
    assert "./start-g3ku.sh" in startup_section
    assert "g3ku web" not in startup_section
    assert "g3ku worker" not in startup_section
    assert "g3ku web" in developer_section
    assert "g3ku worker" in developer_section


def test_readme_adds_emoji_to_seven_pillars_and_feature_intro_section() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "1. 🧠 **自进化体系**" in readme
    assert "2. 🧩  **渐进式加载模式**" in readme
    assert "3. 👥  **多 Agent 架构**" in readme
    assert "4. 🗺️  **混合 Agent 执行模式**" in readme
    assert "5. 🗜️  **多层上下文压缩优化机制**" in readme
    assert "6. ⚡  **性能监控与动态放行机制**" in readme
    assert "7. 🛡️  **安全机制**" in readme

    assert "## 5. 功能介绍" in readme
    assert "如果你第一次接触 G3KU" in readme
    assert "直接问 Agent“你能做什么？有哪些技能和工具？”" in readme
    assert "在 Skill 管理和 Tool 管理页面里自定义管理能力" in readme
    assert "浏览器自动化相关能力" in readme
    assert "定时任务" in readme
    assert "Skill 安装与下载等扩展能力" in readme
