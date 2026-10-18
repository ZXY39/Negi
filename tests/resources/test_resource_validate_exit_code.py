"""`negi resource validate` 的退出码合同：坏资源树必须以非零收尾。

历史缺陷：命令把 errors 与 warnings 混在一起只打印，任何情况都 return None ⇒ exit 0。
脚本与 CI 拿它当闸门时，资源树报错与干净一模一样。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from g3ku.shells import resource_cli


def _invoke(monkeypatch, *, errors: list[str], warnings: list[str] = []) -> int:
    descriptor = SimpleNamespace(name="demo_tool", errors=list(errors), warnings=list(warnings))
    snapshot = SimpleNamespace(tools={"demo_tool": descriptor}, skills={}, generation=1)
    monkeypatch.setattr(resource_cli, "load_config", lambda: SimpleNamespace(workspace_path=Path.cwd()))
    monkeypatch.setattr(
        resource_cli,
        "get_shared_resource_manager",
        lambda *args, **kwargs: SimpleNamespace(reload_now=lambda trigger: snapshot),
    )
    app = resource_cli.build_resource_app(SimpleNamespace(print=lambda *args, **kwargs: None))
    return CliRunner(mix_stderr=False).invoke(app, ["validate"]).exit_code


def test_validate_exits_nonzero_when_a_resource_reports_errors(monkeypatch) -> None:
    assert _invoke(monkeypatch, errors=["manifest kind must be 'skill' or 'tool'"]) == 1


def test_validate_treats_warnings_as_passing(monkeypatch) -> None:
    """警告只提示：资源树带着建议照样算通过。"""
    assert _invoke(monkeypatch, errors=[], warnings=["description is empty"]) == 0


def test_validate_reports_success_only_when_nothing_was_raised(monkeypatch) -> None:
    assert _invoke(monkeypatch, errors=[], warnings=[]) == 0
