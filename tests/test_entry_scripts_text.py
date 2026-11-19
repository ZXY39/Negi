from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_vbs_hidden_wrapper_exists_and_forwards():
    text = (REPO_ROOT / "negi-start.vbs").read_text(encoding="utf-8")
    assert "negi.cmd" in text
    assert ", 0, False" in text  # window style 0 = hidden, no wait
    assert "WScript.Arguments" in text


def test_start_negi_cmd_probes_runnability_and_falls_back():
    text = (REPO_ROOT / "start-negi.cmd").read_text(encoding="utf-8")
    assert "sys.version_info >= (3, 11)" in text
    assert ":run_py_launcher" in text
    assert ":run_python_path" in text
    assert "web -BindHost 127.0.0.1 -Port 18790 %*" in text


def test_install_ps1_stub_delegates_to_project_cmd():
    text = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
    assert 'call "%G3KU_HOME%\\negi.cmd" %*' in text
    assert '"%NEGI_PYTHON%" "%G3KU_HOME%\\negi_bootstrap.py" %*' not in text


def test_install_ps1_icons_use_vbs():
    text = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "negi-start.vbs" in text
    assert "$link.TargetPath = $pythonw" not in text


def test_install_sh_stub_delegates_to_negi_sh():
    text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'exec /bin/sh "\\$G3KU_HOME/negi.sh" "\\$@"' in text
    assert 'exec "\\$NEGI_PYTHON" "\\$G3KU_HOME/negi_bootstrap.py" "\\$@"' not in text
