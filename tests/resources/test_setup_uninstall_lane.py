r"""The Windows uninstall lane must never wait on a window nobody can see.

Negi-Setup.exe's uninstaller ran ``uninstall.ps1`` as a hidden child and the script asked
"keep or purge?" with a modal dialog. ``runhidden`` hides the child's first GUI window too,
so the dialog was created with ``IsWindowVisible=False`` (measured on a device that hit this:
the question window sat at rect (532,301)-(1068,550) on a 1600x900 screen, invisible, while
``unins000.exe`` waited for it and the wizard's progress page showed nothing). The uninstall
then hangs forever, deleting nothing.

The contract these cases hold: the question lives in the wizard's own thread, the script is
purely non-interactive, and the wizard reads its exit code.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ISS = REPO_ROOT / "packaging" / "negi.iss"
UNINSTALL_PS1 = REPO_ROOT / "uninstall.ps1"


def _iss() -> str:
    return ISS.read_text(encoding="utf-8")


def _ps1() -> str:
    return UNINSTALL_PS1.read_text(encoding="utf-8-sig")


def test_wizard_holds_the_data_question_in_its_own_thread() -> None:
    iss = _iss()
    # No hidden child that could be asked to host a dialog.
    assert "[UninstallRun]" not in iss
    assert "RunOnceId" not in iss
    # The wizard still runs the same script, by path, from its own thread.
    assert "ExpandConstant('{app}\\uninstall.ps1')" in iss
    # The uninstaller asks, and its three answers are the three outcomes.
    assert "function InitializeUninstall(): Boolean;" in iss
    assert "MB_YESNOCANCEL" in iss
    assert "IDCANCEL" in iss
    # A silent uninstall has nobody to click, so the default is taken from the documented
    # branch and never from an auto-answered dialog.
    assert "UninstallSilent" in iss
    assert "WizardSilent" not in iss  # calling it during uninstall is a fatal internal error
    # Inno 6.7.3 has no CancelUninstall, so the only clean abort is InitializeUninstall.
    stage = iss[iss.index("procedure CurUninstallStepChanged"):]
    assert "usUninstall" in stage
    assert "ewWaitUntilTerminated" in stage
    assert "Code <> 0" in stage


def test_wizard_hands_the_script_an_explicit_data_flag() -> None:
    iss = _iss()
    assert "UninstallDataFlag := '-KeepData'" in iss
    assert "UninstallDataFlag := '-PurgeData'" in iss
    stage = iss[iss.index("procedure CurUninstallStepChanged"):]
    # The flag is part of the command line, so the script can never reach a question.
    assert "UninstallScriptPath + '\" ' + UninstallDataFlag" in stage


def test_wizard_reports_a_failed_uninstall_without_blocking_a_silent_run() -> None:
    iss = _iss()
    assert "procedure ReportUninstallProblem" in iss
    helper = iss[iss.index("procedure ReportUninstallProblem"):iss.index("function AskUninstallDataFlag")]
    assert "Log(" in helper
    assert "if UninstallSilent then" in helper
    assert "MsgBox(Text, mbError, MB_OK)" in helper
    # Both problem sites go through it; a raw mbError during /SILENT waits for an OK click.
    assert iss.count("ReportUninstallProblem(") >= 3
    stage = iss[iss.index("procedure CurUninstallStepChanged"):]
    assert "MsgBox(" not in stage


def test_uninstaller_opens_no_window_and_reads_no_console() -> None:
    text = _ps1()
    for interactive in ("Windows.Forms", "System.Drawing", "ShowDialog", "Read-Host",
                        "Add-Type", "InputBox", "$Host.UI.RawUI"):
        assert interactive not in text, interactive


def test_uninstaller_decides_from_switches_only() -> None:
    text = _ps1()
    # keep is the default, and destroying data takes the explicit switch.
    assert "$decision = 'keep'" in text
    assert "if ($PurgeData) { $decision = 'purge' }" in text
    assert "$decision = 'cancel'" not in text


def test_uninstaller_ends_every_process_that_locks_this_root() -> None:
    text = _ps1()
    detector = text[text.index("function Get-NegiManagedProcesses"):text.index("function Stop-NegiRuntime")]
    for shape in ("negi_bootstrap\\.py", "-m\\s+g3ku\\s+web", "-m\\s+g3ku\\s+worker",
                  "-m\\s+g3ku\\s+window", "-m\\s+g3ku\\s+mcp"):
        assert shape in detector, shape
    assert "rootPattern" in detector
    stop = text[text.index("function Stop-NegiRuntime"):text.index("function Assert-DeletablePath")]
    assert "taskkill.exe /T /F" in stop
    # taskkill writes to stderr in the machine code page; piping it under ErrorActionPreference
    # =Stop promotes it into a terminating error and aborts the uninstall over a dead process.
    assert "2>$null" in stop
    assert "$LASTEXITCODE" in stop


def test_uninstaller_writes_a_log_that_survives_the_deletion() -> None:
    text = _ps1()
    assert "$LogFile = Join-Path $env:TEMP" in text
    assert "Add-Content -LiteralPath $LogFile" in text
    # the path is printed even on the failure paths
    assert "} finally {\n    Write-Step \"log: $LogFile\"" in text


def test_catch_blocks_name_the_captured_item_not_the_error_record() -> None:
    # Set-StrictMode makes $_.FullName inside a catch a terminating error, which aborted a
    # real uninstall over one in-use file (the uninstaller's own .dat), mid-tree.
    text = _ps1()
    keeper = text[text.index("function Remove-ProgramKeepingData"):]
    assert "$item = $_" in keeper
    assert "$_.FullName" not in keeper
