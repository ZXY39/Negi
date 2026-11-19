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
