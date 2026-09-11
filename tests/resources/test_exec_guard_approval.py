"""exec 命令形态守卫 + 白名单豁免 + 操作者审批的集成测试。

管线契约（评审定稿）：
① full_access 跳过全部（管理端特批模式）；
② 路径监禁层（path policy + workspace 边界）永不可豁免；
③ 命令形态层（只读约束 + deny 黑名单）命中 → 白名单豁免 → 操作者审批
   等待（超时自动拒绝、防刷屏快拒）→ 拒绝并给出引导。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import g3ku.agent.tools.shell as shell_module
from g3ku.agent.tools.shell import ExecTool
from main.governance.exec_approvals import ExecApprovalService
from main.governance.store import GovernanceStore


class _StubProcess:
    returncode = 0

    async def communicate(self):
        return (b"done\n", b"")

    async def wait(self):
        return 0

    def kill(self):
        return None


@pytest.fixture()
def stub_exec(monkeypatch):
    calls: list[list] = []

    async def _fake_create_subprocess_exec(*args, **kwargs):
        calls.append(list(args))
        return _StubProcess()

    monkeypatch.setattr(shell_module.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    return calls


@pytest.fixture()
def approvals(tmp_path: Path):
    store = GovernanceStore(tmp_path / "governance.sqlite3")
    service = ExecApprovalService(store)
    yield service
    store.close()


def _make_tool(tmp_path: Path, approvals_service, **kwargs) -> ExecTool:
    task_service = SimpleNamespace(exec_approvals=approvals_service)
    return ExecTool(
        workspace_root=str(tmp_path),
        main_task_service=task_service,
        **kwargs,
    )


_TASK_RUNTIME = {"session_key": "web:shared", "task_id": "task:1", "actor_role": "execution"}


@pytest.mark.asyncio
async def test_whitelist_entry_exempts_deny_and_readonly_layers(tmp_path, approvals, stub_exec) -> None:
    approvals.add_whitelist_entry(pattern="rm -rf temp/*", scope="tasks")
    tool = _make_tool(tmp_path, approvals)

    payload = json.loads(await tool.execute(command="rm -rf temp/cache_42", __g3ku_runtime=dict(_TASK_RUNTIME)))

    assert payload["status"] == "success", payload
    assert len(stub_exec) == 1


@pytest.mark.asyncio
async def test_whitelist_scope_ceo_does_not_exempt_task_node(tmp_path, approvals, stub_exec) -> None:
    approvals.add_whitelist_entry(pattern="rm -rf temp/*", scope="ceo")
    approvals.set_approval_wait_seconds(5)
    tool = _make_tool(tmp_path, approvals)

    # 任务节点不在 ceo 作用域内 → 走审批等待；无人裁决 → 超时拒绝。
    async def _fast_expire():
        await asyncio.sleep(0.2)
        pending = approvals._store.list_exec_approvals(status="pending")
        for item in pending:
            approvals._store.decide_exec_approval(str(item["approval_id"]), status="denied", decided_by="test")

    expiry = asyncio.create_task(_fast_expire())
    payload = json.loads(await tool.execute(command="rm -rf temp/cache_42", __g3ku_runtime=dict(_TASK_RUNTIME)))
    await expiry

    assert payload["status"] == "error"
    # rm 命令先命中只读层（形态层两源之一），审批被拒后文本含裁决说明。
    assert "read-only tool" in payload["error"].lower()
    assert "拒绝" in payload["error"]
    assert stub_exec == []


@pytest.mark.asyncio
async def test_approval_approve_once_executes_command(tmp_path, approvals, stub_exec) -> None:
    tool = _make_tool(tmp_path, approvals)

    async def _operator_approves():
        for _ in range(100):
            pending = approvals._store.list_exec_approvals(status="pending")
            if pending:
                approvals.resolve(str(pending[0]["approval_id"]), decision="approve_once", decided_by="op")
                return
            await asyncio.sleep(0.05)

    approver = asyncio.create_task(_operator_approves())
    payload = json.loads(await tool.execute(command="rm -rf temp/build_1", __g3ku_runtime=dict(_TASK_RUNTIME)))
    await approver

    assert payload["status"] == "success", payload
    assert len(stub_exec) == 1


@pytest.mark.asyncio
async def test_approval_approve_whitelist_persists_entry(tmp_path, approvals, stub_exec) -> None:
    tool = _make_tool(tmp_path, approvals)

    async def _operator_approves_with_whitelist():
        for _ in range(100):
            pending = approvals._store.list_exec_approvals(status="pending")
            if pending:
                approvals.resolve(
                    str(pending[0]["approval_id"]),
                    decision="approve_whitelist",
                    decided_by="op",
                    scope="tasks",
                )
                return
            await asyncio.sleep(0.05)

    approver = asyncio.create_task(_operator_approves_with_whitelist())
    first = json.loads(await tool.execute(command="rm -rf temp/build_2", __g3ku_runtime=dict(_TASK_RUNTIME)))
    await approver
    assert first["status"] == "success", first

    entries = approvals.list_whitelist()
    assert [item["pattern"] for item in entries] == ["rm -rf temp/build_2"]

    # 第二条同模式命令无需再审批（但它是不同字面命令，不命中精确模板 → 仍走审批；
    # 这里验证白名单对同命令直接放行）。
    second = json.loads(await tool.execute(command="rm -rf temp/build_2", __g3ku_runtime=dict(_TASK_RUNTIME)))
    assert second["status"] == "success"
    assert len(stub_exec) == 2


@pytest.mark.asyncio
async def test_approval_timeout_auto_rejects(tmp_path, approvals, stub_exec, monkeypatch) -> None:
    tool = _make_tool(tmp_path, approvals)
    monkeypatch.setattr(approvals, "get_approval_wait_seconds", lambda: 0.6)

    payload = json.loads(await tool.execute(command="rm -rf temp/build_3", __g3ku_runtime=dict(_TASK_RUNTIME)))

    assert payload["status"] == "error"
    assert "超时" in payload["error"]
    assert "filesystem_delete" in payload["error"], "拒绝文本必须给出专用工具替代引导"
    assert stub_exec == []
    pending = approvals._store.list_exec_approvals(status="pending")
    assert pending == [], "超时后请求必须落 expired 终态"


@pytest.mark.asyncio
async def test_fast_reject_after_repeated_unapproved_attempts(tmp_path, approvals, stub_exec, monkeypatch) -> None:
    tool = _make_tool(tmp_path, approvals)
    monkeypatch.setattr(approvals, "get_approval_wait_seconds", lambda: 0.5)

    for _ in range(2):
        payload = json.loads(await tool.execute(command="rm -rf temp/spam", __g3ku_runtime=dict(_TASK_RUNTIME)))
        assert payload["status"] == "error"

    payload = json.loads(await tool.execute(command="rm -rf temp/spam", __g3ku_runtime=dict(_TASK_RUNTIME)))
    assert payload["status"] == "error"
    assert "不再发起等待" in payload["error"], "近窗连续未获批必须快拒防刷屏"
    assert stub_exec == []


@pytest.mark.asyncio
async def test_path_jail_is_never_exempted_by_whitelist(tmp_path, approvals, stub_exec) -> None:
    approvals.add_whitelist_entry(pattern="rm -rf *", scope="all")
    tool = _make_tool(tmp_path, approvals, restrict_to_workspace=True)
    outside = tmp_path.parent / "outside_dir"

    payload = json.loads(
        await tool.execute(
            command="rm -rf x",
            working_dir=str(outside),
            __g3ku_runtime=dict(_TASK_RUNTIME),
        )
    )

    assert payload["status"] == "error"
    assert "outside workspace" in payload["error"].lower()
    assert stub_exec == []
    assert approvals._store.list_exec_approvals(status="pending") == [], "路径监禁层不得发起审批"
