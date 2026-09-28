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

    # 没点名版本时 apply 会现场查一次：查得到 tag 但 newer=false 才算"已是最新"。
    checks = []

    def _check(**kwargs):
        checks.append(kwargs)
        return {"latest_tag": "v1.0.2", "newer": False, "error": ""}

    monkeypatch.setattr(update_api, "run_update_check", _check)

    with pytest.raises(HTTPException) as nothing:
        await update_api.update_apply(request, {})
    assert nothing.value.status_code == 409
    assert nothing.value.detail["code"] == "no_update_available"

    result = await update_api.update_apply(request, {"ref": "v1.0.2", "pause_running_work": False})
    assert result["item"]["restarting"] is True
    assert spawned[0][0] == "v1.0.2"
    assert spawned[0][1] == 18999, "执行体必须拿到这次请求真正到达的端口"
    assert spawned[0][2] is False, "用户的未确认决定要传到执行体"
    assert len(checks) == 1, "点名了版本就不该再查远端"


async def test_apply_upgrades_to_the_fresh_tag_not_the_stale_ledger(monkeypatch, tmp_path: Path):
    """重启后台账要等满一个检查间隔才刷新，陈旧 latest_tag 不许当升级目标。

    实盘：v1.0.10 已发布，12:58 那次 apply 照 11:32 的台账升 v1.0.9，会把带着
    桥修复的树换回没有修复的旧版。
    """
    import json

    import main.api.update_rest as update_api

    ledger_file = tmp_path / "update-check.json"
    ledger_file.write_text(
        json.dumps(
            {
                "checked_at": "2026-09-28T11:32:18+08:00",
                "current_version": "1.0.8",
                "latest_tag": "v1.0.9",
                "newer": True,
                "source": "manual",
                "error": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("g3ku.update_check.ledger_path", lambda: ledger_file)
    assert read_update_ledger()["latest_tag"] == "v1.0.9", "陈旧台账确实在场"

    spawned = []
    monkeypatch.setattr(
        update_api,
        "spawn_runner",
        lambda ref, port=None, pause_running_work=True: spawned.append(ref),
    )
    monkeypatch.setattr(
        update_api,
        "run_update_check",
        lambda **kwargs: {"latest_tag": "v1.0.10", "newer": True, "error": ""},
    )

    result = await update_api.update_apply(_FakeRequest(18999), {})
    assert result["item"]["ref"] == "v1.0.10"
    assert spawned == ["v1.0.10"]


async def test_apply_refuses_when_the_live_check_returns_nothing(monkeypatch, tmp_path: Path):
    """查不到远端版本 ≠ 照旧台账动手：中止并把原因讲出来（台账三态同一条契约）。"""
    from fastapi import HTTPException

    import main.api.update_rest as update_api

    spawned = []
    monkeypatch.setattr(
        update_api,
        "spawn_runner",
        lambda ref, port=None, pause_running_work=True: spawned.append(ref),
    )
    monkeypatch.setattr(
        update_api,
        "run_update_check",
        lambda **kwargs: {"latest_tag": "", "newer": False, "error": "remote_unreachable"},
    )

    with pytest.raises(HTTPException) as exc:
        await update_api.update_apply(_FakeRequest(18999), {})
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "check_failed"
    assert spawned == [], "查不到就不许踢执行体"


async def test_apply_keeps_service_up_when_spawn_fails(monkeypatch, tmp_path: Path):
    from fastapi import HTTPException

    import main.api.update_rest as update_api

    def _boom(ref, port=None, pause_running_work=True):
        raise OSError("spawn refused")

    monkeypatch.setattr(update_api, "spawn_runner", _boom)
    with pytest.raises(HTTPException) as exc:
        await update_api.update_apply(_FakeRequest(18999), {"ref": "v1.0.2"})
    assert exc.value.status_code == 503


def test_upgrade_output_lands_in_the_log_not_in_a_window(monkeypatch, tmp_path: Path):
    """升级子进程的输出必须实时进日志：捕获到结束才落盘的话，慢网络下用户看到的
    就是一个空窗口 + 一行 upgrade start，没有任何判据（实盘就是这句）。"""
    import sys

    import g3ku.update_apply as apply_mod

    log_file = tmp_path / "update-apply.log"
    monkeypatch.setattr(apply_mod, "LOG_FILE", log_file)
    monkeypatch.setattr(
        apply_mod,
        "_upgrade_command",
        lambda ref: [sys.executable, "-c", "print('正在解析依赖 🥬')"],
    )

    assert apply_mod._run_upgrade("v9.9.9") is True
    text = log_file.read_text(encoding="utf-8")
    assert "正在解析依赖" in text
    assert "upgrade exit=0" in text


def test_upgrade_reports_nonzero_instead_of_claiming_success(monkeypatch, tmp_path: Path):
    import sys

    import g3ku.update_apply as apply_mod

    log_file = tmp_path / "update-apply.log"
    monkeypatch.setattr(apply_mod, "LOG_FILE", log_file)
    monkeypatch.setattr(
        apply_mod,
        "_upgrade_command",
        lambda ref: [sys.executable, "-c", "import sys; sys.exit(3)"],
    )

    assert apply_mod._run_upgrade("v9.9.9") is False
    assert "upgrade exit=3" in log_file.read_text(encoding="utf-8")


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


def test_relaunched_web_output_lands_in_the_app_log_not_the_apply_log(tmp_path: Path, monkeypatch):
    """重拉起的 web 不许把访问日志写进 update-apply.log。

    实盘：12:58 那次 apply 的整张时间表被后面几万行 INFO 埋住，判读得先按时间戳
    行过滤才捞得出来。执行体的锚点与服务的输出是两个流，各去各的文件。
    """
    import g3ku.update_apply as apply_mod

    apply_log = tmp_path / "update-apply.log"
    web_log = tmp_path / "console.log"
    monkeypatch.setattr(apply_mod, "LOG_FILE", apply_log)
    monkeypatch.setattr(apply_mod, "WEB_LOG_FILE", web_log)

    handles = []
    monkeypatch.setattr(
        apply_mod.subprocess, "Popen", lambda cmd, **kwargs: handles.append(kwargs["stdout"])
    )

    apply_mod._relaunch_web(18790)
    assert Path(handles[-1].name).name == "console.log"
    anchors = apply_log.read_text(encoding="utf-8")
    assert "relaunch web -m g3ku web --port 18790" in anchors, "执行体自己的锚点仍要留在这里"

    apply_mod.spawn_runner("v1.0.2", port=18790)
    assert Path(handles[-1].name).name == "update-apply.log", "执行体自身的输出不许跑偏"


def _result_file(tmp_path: Path, monkeypatch) -> Path:
    from g3ku.update_check import apply_result_path  # noqa: F401 - 只证符号存在

    target = tmp_path / "update-apply-result.json"
    monkeypatch.setattr("g3ku.update_check.apply_result_path", lambda: target)
    return target


def test_apply_records_a_refused_exit_as_its_own_terminal_outcome(tmp_path: Path, monkeypatch):
    """执行体停在"服务拒绝退出"时，web 侧要能看见，而不是只剩 restarting。"""
    import json

    import g3ku.update_apply as apply_mod

    result_file = _result_file(tmp_path, monkeypatch)
    monkeypatch.setattr(apply_mod, "LOG_FILE", tmp_path / "apply.log")
    monkeypatch.setattr(apply_mod, "STARTUP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(apply_mod, "_probe_self", lambda port: True)
    monkeypatch.setattr(apply_mod, "_request_exit", lambda port, pause: "exit_refused_409")
    monkeypatch.setattr(
        apply_mod, "_run_upgrade", lambda ref: (_ for _ in ()).throw(AssertionError("被拒不许动代码"))
    )

    assert apply_mod.run("v1.0.12", port=18999) == 2
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["outcome"] == "exit_refused"
    assert data["ref"] == "v1.0.12"
    assert data["detail"] == "exit_refused_409"


def test_failed_upgrade_is_recorded_even_though_the_service_comes_back(tmp_path: Path, monkeypatch):
    """升级失败但旧版本已重拉起：终态是 upgrade_failed，不是 ok。"""
    import json

    import g3ku.update_apply as apply_mod

    result_file = _result_file(tmp_path, monkeypatch)
    relaunched = []
    monkeypatch.setattr(apply_mod, "LOG_FILE", tmp_path / "apply.log")
    monkeypatch.setattr(apply_mod, "STARTUP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(apply_mod, "_probe_self", lambda port: True)
    monkeypatch.setattr(apply_mod, "_request_exit", lambda port, pause: "exit_accepted_200")
    monkeypatch.setattr(apply_mod, "_wait_for_release", lambda port: True)
    monkeypatch.setattr(apply_mod, "_run_upgrade", lambda ref: False)
    monkeypatch.setattr(apply_mod, "_relaunch_web", lambda port: relaunched.append(port))

    assert apply_mod.run("v1.0.12", port=18790) == 1
    assert relaunched == [18790], "失败也要把服务拉回来"
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["outcome"] == "upgrade_failed"
    assert "exit_accepted_200" in data["detail"]


def test_spawn_runner_marks_the_attempt_as_started(tmp_path: Path, monkeypatch):
    import json

    import g3ku.update_apply as apply_mod

    result_file = _result_file(tmp_path, monkeypatch)
    spawned = []
    monkeypatch.setattr(apply_mod, "_spawn_detached", lambda *args, **kwargs: spawned.append(args))

    apply_mod.spawn_runner("v1.0.12", port=18790)

    assert spawned, "执行体已踢起"
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["outcome"] == "started"
    assert data["ref"] == "v1.0.12"


async def test_status_endpoint_carries_the_last_apply_outcome(monkeypatch, tmp_path: Path):
    import json

    import main.api.update_rest as update_api

    monkeypatch.setattr("g3ku.update_check.ledger_path", lambda: tmp_path / "update-check.json")
    result_file = _result_file(tmp_path, monkeypatch)
    result_file.write_text(
        json.dumps(
            {"at": "2026-09-28T13:19:05+08:00", "ref": "v1.0.12", "outcome": "port_busy", "detail": "x"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = update_api._status_payload()
    assert payload["last_apply"]["outcome"] == "port_busy"
