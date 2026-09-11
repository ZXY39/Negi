"""exec 命令白名单与操作者审批服务的单元测试。

覆盖：模板归一化/锚定匹配、过宽拒绝、scope 判定、白名单 CRUD、审批
等待时长设置、审批生命周期（预裁决/超时过期/取消/防刷屏快拒）、
approve_whitelist 的白名单落库联动。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from main.governance.exec_approvals import (
    APPROVAL_FAST_REJECT_THRESHOLD,
    DEFAULT_APPROVAL_WAIT_SECONDS,
    ExecApprovalService,
    compile_command_template,
    is_pattern_too_broad,
    normalize_command,
    scope_allows,
    template_to_regex_src,
)
from main.governance.store import GovernanceStore


@pytest.fixture()
def service(tmp_path: Path) -> ExecApprovalService:
    store = GovernanceStore(tmp_path / "governance.sqlite3")
    yield ExecApprovalService(store)
    store.close()


# -- 模板与匹配 -----------------------------------------------------------


def test_normalize_command_collapses_whitespace() -> None:
    assert normalize_command("  rm   -rf \t temp/ ") == "rm -rf temp/"


def test_template_matches_literal_with_whitespace_tolerance() -> None:
    compiled = compile_command_template("rm -rf temp/cache")
    assert compiled.fullmatch("rm -rf temp/cache")
    assert compiled.fullmatch("rm   -rf    temp/cache"), "空白归一化后应命中"
    assert not compiled.fullmatch("rm -rf temp/cache2"), "锚定匹配不允许后缀延伸"
    assert not compiled.fullmatch("echo hi && rm -rf temp/cache"), "锚定匹配不允许复合前缀"


def test_template_wildcard_and_case_insensitive() -> None:
    compiled = compile_command_template("rm -rf temp/*")
    assert compiled.fullmatch("rm -rf temp/cache_20260911")
    assert compiled.fullmatch("RM -RF temp/x/y"), "大小写不敏感"
    assert not compiled.fullmatch("rm -rf /home/data")

    middle = compile_command_template("git * push")
    assert middle.fullmatch("git -C repo push")
    assert not middle.fullmatch("git push"), "中段通配需有内容"


def test_template_regex_src_is_anchored() -> None:
    src = template_to_regex_src("ls -la")
    assert src.startswith("^") and src.endswith("$")


def test_is_pattern_too_broad() -> None:
    assert is_pattern_too_broad("*")
    assert is_pattern_too_broad("rm")
    assert is_pattern_too_broad("")
    assert not is_pattern_too_broad("rm -rf temp/*")
    assert not is_pattern_too_broad("git push origin main")


def test_scope_allows_matrix() -> None:
    assert scope_allows("all", "execution")
    assert scope_allows("all", "ceo")
    assert scope_allows("ceo", "ceo")
    assert not scope_allows("ceo", "execution")
    assert scope_allows("tasks", "execution")
    assert scope_allows("tasks", "inspection")
    assert not scope_allows("tasks", "ceo")


# -- 白名单 CRUD ----------------------------------------------------------


def test_whitelist_add_list_remove_and_duplicate(service: ExecApprovalService) -> None:
    entry = service.add_whitelist_entry(
        pattern="rm  -rf   temp/*", scope="tasks", created_by="tester", reason="清理任务临时目录"
    )
    assert entry["pattern"] == "rm -rf temp/*", "入库前空白归一化"
    assert [item["pattern"] for item in service.list_whitelist()] == ["rm -rf temp/*"]

    with pytest.raises(ValueError, match="pattern_already_exists"):
        service.add_whitelist_entry(pattern="rm -rf temp/*", scope="tasks")

    assert service.remove_whitelist_entry(pattern="rm -rf temp/*", scope="tasks") is True
    assert service.list_whitelist() == []
    assert service.remove_whitelist_entry(pattern="rm -rf temp/*") is False


def test_whitelist_rejects_broad_or_invalid(service: ExecApprovalService) -> None:
    with pytest.raises(ValueError, match="pattern_too_broad"):
        service.add_whitelist_entry(pattern="rm")
    with pytest.raises(ValueError, match="pattern_required"):
        service.add_whitelist_entry(pattern="   ")
    with pytest.raises(ValueError, match="scope_invalid"):
        service.add_whitelist_entry(pattern="git push origin *", scope="everyone")


def test_command_allowed_respects_scope_and_anchoring(service: ExecApprovalService) -> None:
    service.add_whitelist_entry(pattern="rm -rf temp/*", scope="tasks")
    assert service.command_allowed("rm -rf temp/cache", actor_role="execution") is not None
    assert service.command_allowed("rm -rf temp/cache", actor_role="ceo") is None, "tasks 作用域不放行 CEO"
    assert service.command_allowed("rm -rf /etc", actor_role="execution") is None
    assert service.command_allowed("", actor_role="execution") is None


# -- 审批等待时长 ----------------------------------------------------------


def test_approval_wait_seconds_default_and_clamp(service: ExecApprovalService) -> None:
    assert service.get_approval_wait_seconds() == DEFAULT_APPROVAL_WAIT_SECONDS
    assert service.set_approval_wait_seconds(45) == 45.0
    assert service.get_approval_wait_seconds() == 45.0
    assert service.set_approval_wait_seconds(1) == 5.0, "低于下限抬到 5s"
    assert service.set_approval_wait_seconds(99999) == 3600.0, "高于上限压到 3600s"


# -- 审批生命周期 ----------------------------------------------------------


def test_request_then_predecided_approval_returns_immediately(service: ExecApprovalService) -> None:
    created = service.request_approval(
        command="rm -rf temp/cache",
        guard_reason="dangerous pattern",
        actor_role="execution",
        lane="task",
        context_id="task:1",
    )
    assert created is not None
    result = service.resolve(created["approval_id"], decision="approve_once", decided_by="op")
    assert result["status"] == "approved_once" and result["duplicate"] is False

    async def _wait() -> str:
        return await service.wait_for_decision(created["approval_id"], timeout_seconds=5.0)

    assert asyncio.run(_wait()) == "approved_once"


def test_wait_times_out_and_marks_expired(service: ExecApprovalService) -> None:
    created = service.request_approval(
        command="rm -rf temp/x", guard_reason="r", actor_role="execution", lane="task", context_id="t:2"
    )
    assert created is not None

    async def _wait() -> str:
        return await service.wait_for_decision(created["approval_id"], timeout_seconds=0.6, poll_interval_seconds=0.1)

    assert asyncio.run(_wait()) == "expired"


def test_wait_returns_cancelled_when_token_triggered(service: ExecApprovalService) -> None:
    created = service.request_approval(
        command="rm -rf temp/y", guard_reason="r", actor_role="execution", lane="task", context_id="t:3"
    )
    assert created is not None

    class _Token:
        @staticmethod
        def is_cancelled() -> bool:
            return True

    async def _wait() -> str:
        return await service.wait_for_decision(
            created["approval_id"], timeout_seconds=5.0, cancel_token=_Token(), poll_interval_seconds=0.1
        )

    assert asyncio.run(_wait()) == "cancelled"


def test_fast_reject_after_repeated_timeouts(service: ExecApprovalService) -> None:
    for _ in range(APPROVAL_FAST_REJECT_THRESHOLD):
        created = service.request_approval(
            command="rm -rf temp/spam", guard_reason="r", actor_role="execution", lane="task", context_id="t:4"
        )
        assert created is not None

        async def _wait() -> str:
            return await service.wait_for_decision(
                created["approval_id"], timeout_seconds=0.5, poll_interval_seconds=0.1
            )

        assert asyncio.run(_wait()) == "expired"

    blocked = service.request_approval(
        command="rm -rf temp/spam", guard_reason="r", actor_role="execution", lane="task", context_id="t:4"
    )
    assert blocked is None, "近窗连续未获批达阈值后必须快拒，不再发起等待"


def test_resolve_approve_whitelist_adds_scoped_entry(service: ExecApprovalService) -> None:
    created = service.request_approval(
        command="rm   -rf   temp/build_*", guard_reason="r", actor_role="execution", lane="task", context_id="t:5"
    )
    assert created is not None
    result = service.resolve(
        created["approval_id"], decision="approve_whitelist", decided_by="op", scope="tasks", reason="构建清理"
    )
    assert result["status"] == "approved_whitelist"
    entry = result["whitelist_entry"]
    assert entry is not None and entry["pattern"] == "rm -rf temp/build_*" and entry["scope"] == "tasks"
    assert service.command_allowed("rm -rf temp/build_42", actor_role="execution") is not None


def test_resolve_rejects_invalid_decision_and_double_resolve(service: ExecApprovalService) -> None:
    created = service.request_approval(
        command="rm -rf temp/z", guard_reason="r", actor_role="execution", lane="task", context_id="t:6"
    )
    assert created is not None
    with pytest.raises(ValueError, match="decision_invalid"):
        service.resolve(created["approval_id"], decision="maybe")
    first = service.resolve(created["approval_id"], decision="deny")
    assert first["status"] == "denied"
    second = service.resolve(created["approval_id"], decision="approve_once")
    assert second["duplicate"] is True and second["status"] == "denied", "已裁决请求不可被改写"
    with pytest.raises(KeyError):
        service.resolve("missing-id", decision="deny")
