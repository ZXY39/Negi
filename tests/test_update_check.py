from pathlib import Path

import pytest

from g3ku.update_check import (
    REMOTE_UNREACHABLE,
    build_ledger_payload,
    check_is_due,
    fetch_latest_release_tag,
    latest_release_tag,
    parse_version,
    read_update_ledger,
    run_update_check,
)


def test_parse_version_accepts_bare_and_prefixed():
    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version(" v1.2.10 ") == (1, 2, 10)
    assert parse_version("1.2") is None
    assert parse_version("nightly") is None


def test_latest_release_tag_picks_highest_not_newest_line():
    output = "\n".join(
        [
            "aaa111\trefs/tags/v1.0.0",
            "bbb222\trefs/tags/v1.10.0",
            "ccc333\trefs/tags/v1.9.0^{}",
            "ddd444\trefs/tags/v1.2.0",
        ]
    )
    assert latest_release_tag(output) == "v1.10.0"


def test_latest_release_tag_ignores_non_release_refs():
    output = "\n".join(
        [
            "aaa111\trefs/tags/backup/pre-reword-ac670b5b",
            "bbb222\trefs/tags/v1.0.0^{}",
            "ccc333\trefs/heads/main",
            "ddd444\trefs/tags/v2beta",
        ]
    )
    assert latest_release_tag(output) is None


def test_latest_release_tag_handles_empty_output():
    assert latest_release_tag("") is None


def test_fetch_returns_none_without_origin(tmp_path: Path):
    assert fetch_latest_release_tag(tmp_path, timeout=10.0) is None


def test_auto_pass_writes_ledger_and_second_pass_skips_network(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "g3ku.update_check.fetch_latest_release_tag",
        lambda root, timeout=2.0: calls.append(1) or "v9.9.9",
    )
    ledger_file = tmp_path / "update-check.json"

    first = run_update_check(source="auto", interval_seconds=3600.0, ledger_file=ledger_file)
    assert first["newer"] is True
    assert first["latest_tag"] == "v9.9.9"
    assert first["error"] == ""
    assert read_update_ledger(ledger_file)["latest_tag"] == "v9.9.9"

    second = run_update_check(source="auto", interval_seconds=3600.0, ledger_file=ledger_file)
    assert second["checked_at"] == first["checked_at"]
    assert len(calls) == 1, "未到间隔的自动轮次不得联网"

    forced = run_update_check(source="manual", interval_seconds=3600.0, ledger_file=ledger_file, force=True)
    assert forced["source"] == "manual"
    assert len(calls) == 2, "手动检查必须绕开间隔"


def test_unreachable_remote_records_error_not_up_to_date(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("g3ku.update_check.fetch_latest_release_tag", lambda root, timeout=2.0: None)
    payload = run_update_check(source="auto", interval_seconds=1.0, ledger_file=tmp_path / "update-check.json")
    assert payload["latest_tag"] == ""
    assert payload["newer"] is False
    assert payload["error"] == REMOTE_UNREACHABLE
    assert build_ledger_payload(None, source="auto")["error"] == REMOTE_UNREACHABLE


def test_check_is_due_treats_missing_and_malformed_as_due():
    assert check_is_due(None, 3600.0) is True
    assert check_is_due({"checked_at": ""}, 3600.0) is True
    assert check_is_due({"checked_at": "not-a-time"}, 3600.0) is True
    assert check_is_due({"checked_at": "2099-01-01T00:00:00+08:00"}, 3600.0) is False


def _status_payload_with(monkeypatch, tmp_path: Path, payload):
    import main.api.update_rest as update_api

    ledger_file = tmp_path / "update-check.json"
    if payload is not None:
        from g3ku.update_check import write_update_ledger

        write_update_ledger(payload, ledger_file)
    monkeypatch.setattr("g3ku.update_check.ledger_path", lambda: ledger_file)
    return update_api._status_payload()


async def test_status_endpoint_reports_unknown_without_network(monkeypatch, tmp_path: Path):
    item = _status_payload_with(monkeypatch, tmp_path, None)
    assert item["has_ledger"] is False
    assert item["newer"] is False
    assert item["latest_tag"] == ""


async def test_status_endpoint_exposes_ledger(monkeypatch, tmp_path: Path):
    item = _status_payload_with(
        monkeypatch,
        tmp_path,
        {"checked_at": "2026-09-25T17:45:04+08:00", "current_version": "1.0.1", "latest_tag": "v1.0.2", "newer": True, "source": "auto", "error": ""},
    )
    assert item["has_ledger"] is True
    assert item["newer"] is True
    assert item["latest_tag"] == "v1.0.2"


class _FakeURL:
    def __init__(self, port):
        self.port = port


class _FakeRequest:
    """只带端口需求的 Request 替身。"""

    def __init__(self, port):
        self.url = _FakeURL(port)
        self.base_url = _FakeURL(port)


async def test_apply_requires_a_version_shape_and_rejects_when_current(monkeypatch, tmp_path: Path):
    from fastapi import HTTPException

    import main.api.update_rest as update_api

    ledger_file = tmp_path / "update-check.json"
    monkeypatch.setattr("g3ku.update_check.ledger_path", lambda: ledger_file)

    spawned = []
    monkeypatch.setattr(
        update_api,
        "spawn_runner",
        lambda ref, port=None, pause_running_work=True: spawned.append((ref, port, pause_running_work)),
    )
    # 服务实际监听端口与 config.web.port 不同（--port 启动）时的回归场景。
    request = _FakeRequest(18999)

    with pytest.raises(HTTPException) as bad_ref:
        await update_api.update_apply(request, {"ref": "v1; rm -rf /"})
    assert bad_ref.value.status_code == 400
    assert spawned == []

    with pytest.raises(HTTPException) as nothing:
        await update_api.update_apply(request, {})
    assert nothing.value.status_code == 409
    assert nothing.value.detail["code"] == "no_update_available"

    result = await update_api.update_apply(request, {"ref": "v1.0.2", "pause_running_work": False})
    assert result["item"]["restarting"] is True
    assert spawned[0][0] == "v1.0.2"
    assert spawned[0][1] == 18999, "执行体必须拿到这次请求真正到达的端口"
    assert spawned[0][2] is False, "用户的未确认决定要传到执行体"


async def test_apply_keeps_service_up_when_spawn_fails(monkeypatch, tmp_path: Path):
    from fastapi import HTTPException

    import main.api.update_rest as update_api

    def _boom(ref, port=None, pause_running_work=True):
        raise OSError("spawn refused")

    monkeypatch.setattr(update_api, "spawn_runner", _boom)
    with pytest.raises(HTTPException) as exc:
        await update_api.update_apply(_FakeRequest(18999), {"ref": "v1.0.2"})
    assert exc.value.status_code == 503


def test_run_captured_decodes_utf8_child_output(monkeypatch):
    """子进程写 UTF-8 时不能按系统 ANSI 码页解：中文 Windows 上是 gbk，
    解码异常会把整段升级输出吞掉（实盘日志里真炸过）。"""
    import sys

    import g3ku.update_apply as apply_mod

    monkeypatch.setenv("PYTHONUTF8", "1")
    completed = apply_mod._run_captured([sys.executable, "-c", "print('升级完成 🥬')"], Path.cwd(), 60.0)
    assert completed.returncode == 0
    assert "升级完成" in completed.stdout
    assert "🥬" in completed.stdout


def test_upgrade_command_targets_the_running_project_root():
    """漏传目录时安装脚本会退回默认位置，等于升级了另一个目录、重启未变的代码。"""
    import g3ku.update_apply as apply_mod

    command = apply_mod._upgrade_command("v9.9.9")
    assert command is not None
    assert str(apply_mod.PROJECT_ROOT) in command
    assert any(flag in command for flag in ("-Dir", "--dir"))


def test_runner_refuses_to_act_without_a_known_port(tmp_path: Path, monkeypatch):
    """猜端口等于可能关掉同机的另一个实例，所以未知即中止且不发退出请求。"""
    import g3ku.update_apply as apply_mod

    requested = []
    monkeypatch.setattr(apply_mod, "_request_exit", lambda port, pause: requested.append(port) or "exit_accepted_200")
    monkeypatch.setattr(apply_mod, "_read_config_port", lambda: None)
    monkeypatch.setattr(apply_mod, "LOG_FILE", tmp_path / "apply.log")

    assert apply_mod.run("v1.0.2") == 4
    assert requested == []
    assert "port unknown" in (tmp_path / "apply.log").read_text(encoding="utf-8")


def test_runner_refuses_a_port_that_is_not_this_service(tmp_path: Path, monkeypatch):
    """端口上答话的不是本服务（端口取错的后果）⇒ 连退出请求都不发。"""
    import g3ku.update_apply as apply_mod

    requested = []
    monkeypatch.setattr(apply_mod, "_probe_self", lambda port: False)
    monkeypatch.setattr(apply_mod, "_request_exit", lambda port, pause: requested.append(port) or "exit_accepted_200")
    monkeypatch.setattr(apply_mod, "LOG_FILE", tmp_path / "apply.log")

    assert apply_mod.run("v1.0.2", port=18999) == 5
    assert requested == []
    assert "no Negi bootstrap endpoint" in (tmp_path / "apply.log").read_text(encoding="utf-8")


def test_runner_aborts_before_touching_code_when_exit_refused(tmp_path: Path, monkeypatch):
    import g3ku.update_apply as apply_mod

    upgraded = []
    monkeypatch.setattr(apply_mod, "_probe_self", lambda port: True)
    monkeypatch.setattr(apply_mod, "_request_exit", lambda port, pause: "exit_refused_409")
    monkeypatch.setattr(apply_mod, "_run_upgrade", lambda ref: upgraded.append(ref) or True)
    monkeypatch.setattr(apply_mod, "STARTUP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(apply_mod, "LOG_FILE", tmp_path / "apply.log")

    assert apply_mod.run("v1.0.2", port=18999) == 2
    assert upgraded == [], "服务没让停就不许动代码"
