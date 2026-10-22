"""Stage 6：`reuse_args=true` 的整批执行与聚合，以及非法复用的三类回执。

用例从车道进口喂（`ReActToolLoop._execute_tool_raw`），不直接调渲染函数：只有在这个入口
上，"复用不再走契约投递""每份结果各自限界后再聚合""C1/C2 不退预算的同一套记账"这三件事
才同时成立。计划 §4 Stage 6 的验证项逐条对应：

- N 份暂存 ⇒ **单个** tool_result、`items` 长度为 N、顺序等于暂存顺序；
- 其中一份失败 ⇒ 顶层 `ok:true`、该项 `ok:false`；
- 契约不在场 ⇒ 一份都不跑，并把该作用域的暂存清掉（C2）；
- 没有可复用条目 ⇒ C1 文案，且这一跳按"没执行"记账（不退预算的那条通道）；
- `reuse_args` 与显式入参同时给 ⇒ 按显式入参跑一次并说明被忽略（C3）；
- 尾块不再有 `callable_tools`，`denied_tools` 照旧在。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime import tool_arg_stash
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.frontdoor.tool_contract import (
    _render_frontdoor_contract_summary,
)
from g3ku.runtime.tool_contract_block import render_tool_contract_block
from g3ku.runtime.tool_impulse import (
    REUSE_PLAN_CONTRACT_ABSENT,
    REUSE_PLAN_EXECUTE,
    REUSE_PLAN_EXPLICIT_ARGS,
    REUSE_PLAN_NO_STASH,
    plan_reuse_execution,
    split_reuse_arguments,
)
from g3ku.runtime.tool_result_status import is_error_like_tool_result
from main.models import NodeRecord
from main.runtime.node_prompt_contract import (
    _render_node_dynamic_contract_summary,
)
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService

TOOL_ID = "filesystem_write"
TASK_ID = "task-reuse"
NODE_ID = "node-reuse"
FINGERPRINT = "tcf:" + "c" * 16
SCOPE = tool_arg_stash.node_scope_key(TASK_ID, NODE_ID, TOOL_ID)


class _BatchTool(Tool):
    """按 value 记录调用；value 为 "boom" 时抛错，用来制造"一项失败、整批成功"。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def name(self) -> str:
        return TOOL_ID

    @property
    def description(self) -> str:
        return "write"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, value: str = "", **kwargs) -> str:
        _ = kwargs
        self.seen.append(str(value))
        if str(value) == "boom":
            raise RuntimeError("disk full")
        return json.dumps({"ok": True, "value": value}, ensure_ascii=False)


class _SizedBatchTool(_BatchTool):
    """结果大小由 value 控制：big 触发单份外置，mid 让聚合体越过 6000。"""

    async def execute(self, value: str = "", **kwargs) -> str:
        _ = kwargs
        self.seen.append(str(value))
        size = {"big": 7000, "mid1": 3000, "mid2": 3000}.get(str(value), 5)
        return json.dumps({"ok": True, "value": "x" * size, "tag": str(value)}, ensure_ascii=False)


def _service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service.store.upsert_node(
        NodeRecord(
            node_id=NODE_ID,
            task_id=TASK_ID,
            root_node_id=NODE_ID,
            goal="g",
            prompt="p",
            created_at="2026-10-21T00:00:00+08:00",
            updated_at="2026-10-21T00:00:00+08:00",
        )
    )
    return service


def _carrier(*, hop: int, staged: list[dict], scope_key: str = SCOPE) -> dict:
    """用公开 API 造"上一跳刚暂存好 N 份入参"的载体（严格一跳：条目 hop = 当前 hop-1）。"""
    container: dict = {}
    for entry in staged:
        container, _item, _count = tool_arg_stash.stage_args(
            container,
            scope_key=scope_key,
            args=dict(entry),
            fingerprint=FINGERPRINT,
            staged_hop=hop - 1,
        )
    return tool_arg_stash.build_carrier(container, hop=hop)


def _context(*, carrier: dict, contract_present: bool, call_id: str = "call-reuse-1") -> dict:
    return {
        "task_id": TASK_ID,
        "node_id": NODE_ID,
        "actor_role": "execution",
        "session_key": "web:shared",
        "tool_contract_presence": {TOOL_ID: FINGERPRINT} if contract_present else {},
        "tool_arg_stash_carrier": carrier,
        "impulse_delivered_tools": {},
        "impulse_staged_call_ids": {call_id},
        "impulse_batch_totals": {},
        "current_tool_call_id": call_id,
        "stage_turn_granted": True,
    }


def _loop(service: MainRuntimeService) -> ReActToolLoop:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=SimpleNamespace(), max_iterations=2)
    loop._tool_impulse_service = service
    return loop


async def _run(loop: ReActToolLoop, tool: Tool, arguments: dict, context: dict) -> str:
    return await loop._execute_tool_raw(
        tools={TOOL_ID: tool},
        tool_name=TOOL_ID,
        arguments=arguments,
        runtime_context=context,
    )


# ---------------------------------------------------------------------------
# 整批执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reuse_executes_every_staged_set_in_order(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _BatchTool()
    runtime_context = _context(
        carrier=_carrier(hop=4, staged=[{"value": "a"}, {"value": "b"}, {"value": "c"}]),
        contract_present=True,
    )

    content = await _run(loop, tool, {"reuse_args": True}, runtime_context)

    payload = json.loads(content)
    assert payload["ok"] is True
    assert payload["executed"] == 3
    assert payload["failed"] == 0
    assert [item["index"] for item in payload["items"]] == [0, 1, 2]
    assert [item["result"] for item in payload["items"]] == [
        json.dumps({"ok": True, "value": "a"}, ensure_ascii=False),
        json.dumps({"ok": True, "value": "b"}, ensure_ascii=False),
        json.dumps({"ok": True, "value": "c"}, ensure_ascii=False),
    ]
    # 顺序 = 暂存顺序，且一份都没漏、没重复。
    assert tool.seen == ["a", "b", "c"]
    # 消费过就没了：同一跳再复用不会再跑一遍（幂等键不在这里，靠的是条目被取走）。
    assert service.node_stash_carrier(NODE_ID).get(tool_arg_stash.STASH_CONTAINER_KEY, {}).get(SCOPE) in (None, [])


@pytest.mark.asyncio
async def test_one_failing_item_does_not_fail_the_batch(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _BatchTool()
    runtime_context = _context(
        carrier=_carrier(hop=4, staged=[{"value": "a"}, {"value": "boom"}, {"value": "c"}]),
        contract_present=True,
    )

    payload = json.loads(await _run(loop, tool, {"reuse_args": True}, runtime_context))

    assert payload["ok"] is True
    assert payload["executed"] == 3
    assert payload["failed"] == 1
    assert [item["ok"] for item in payload["items"]] == [True, False, True]
    assert "disk full" in str(payload["items"][1]["error"])
    assert "result" not in payload["items"][1]
    assert tool.seen == ["a", "boom", "c"]


@pytest.mark.asyncio
async def test_reuse_delivery_keeps_aggregate_whole(tmp_path: Path) -> None:
    """每份结果各自过外置闸门；聚合体本身整包透传，不变成第二个信封。"""
    service = _service(tmp_path)
    loop = ReActToolLoop(
        chat_backend=SimpleNamespace(),
        log_service=SimpleNamespace(_content_store=service.content_store),
        max_iterations=2,
    )
    loop._tool_impulse_service = service
    tool = _SizedBatchTool()
    runtime_context = _context(
        carrier=_carrier(hop=4, staged=[{"value": "big"}, {"value": "mid1"}, {"value": "mid2"}]),
        contract_present=True,
    )

    raw = await _run(loop, tool, {"reuse_args": True}, runtime_context)
    delivered = loop._render_tool_message_content(
        raw,
        runtime_context=runtime_context,
        tool_name=TOOL_ID,
        delivery_metadata=loop._tool_result_delivery_metadata(
            tools={TOOL_ID: tool},
            tool_name=TOOL_ID,
            arguments={"reuse_args": True},
        ),
    )

    assert len(str(delivered)) > 6000
    assert str(delivered) == str(raw)
    payload = json.loads(str(delivered))
    assert payload["executed"] == 3
    assert payload["failed"] == 0
    # 大份在单次闸门处分、成为自己的信封；两份中份内联进 items。
    assert "result externalized" in payload["items"][0]["result"]
    assert "externalized" not in payload["items"][1]["result"]
    assert "externalized" not in payload["items"][2]["result"]


@pytest.mark.asyncio
async def test_reuse_survives_to_storage_and_cannot_be_consumed_twice(tmp_path: Path) -> None:
    """整批消费必须落到节点行：只改内存的话 pause/resume 后同一批会被再跑一遍。"""
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _BatchTool()
    runtime_context = _context(
        carrier=service.persist_node_stash_carrier(NODE_ID, _carrier(hop=4, staged=[{"value": "a"}, {"value": "b"}])),
        contract_present=True,
    )

    await _run(loop, tool, {"reuse_args": True}, runtime_context)
    assert tool.seen == ["a", "b"]

    second = await _run(loop, tool, {"reuse_args": True}, _context(carrier=service.node_stash_carrier(NODE_ID), contract_present=True))
    assert tool.seen == ["a", "b"]
    assert "No staged arguments" in second


# ---------------------------------------------------------------------------
# 非法复用（C1/C2/C3）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reuse_without_contract_present_runs_nothing_and_drops_stash(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _BatchTool()
    carrier = service.persist_node_stash_carrier(NODE_ID, _carrier(hop=4, staged=[{"value": "a"}]))
    runtime_context = _context(carrier=carrier, contract_present=False)

    content = await _run(loop, tool, {"reuse_args": True}, runtime_context)

    assert "left the context" in content
    assert tool.seen == []
    assert service.node_stash_carrier(NODE_ID).get(tool_arg_stash.STASH_CONTAINER_KEY, {}).get(SCOPE) in (None, [])
    # C2 不占预算：这一跳被记成"没执行"，由既有的整轮退还通道处理。
    assert runtime_context["impulse_staged_call_ids"] == {runtime_context["current_tool_call_id"]}


@pytest.mark.asyncio
async def test_reuse_with_nothing_staged_says_c1(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _BatchTool()
    runtime_context = _context(carrier=tool_arg_stash.build_carrier({}, hop=9), contract_present=True)

    content = await _run(loop, tool, {"reuse_args": True}, runtime_context)

    assert content.startswith("No staged arguments")
    assert TOOL_ID in content
    assert tool.seen == []


@pytest.mark.asyncio
async def test_explicit_arguments_win_and_are_reported(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _BatchTool()
    runtime_context = _context(
        carrier=service.persist_node_stash_carrier(NODE_ID, _carrier(hop=4, staged=[{"value": "staged"}])),
        contract_present=True,
    )

    content = await _run(loop, tool, {"reuse_args": True, "value": "explicit"}, runtime_context)

    assert content.startswith("reuse_args ignored")
    assert tool.seen == ["explicit"]
    # 形状记两格：打了标记又被显式参数顶掉 ≠ 根本没打标记；参数与暂存不同 ⇒ new_args。
    assert runtime_context["tool_reuse_flag_ignored"] == {"call-reuse-1": True}
    assert runtime_context["tool_staged_replay"] == {"call-reuse-1": "new_args"}
    # 被忽略的暂存不留着：下一次 reuse_args 会跑出模型以为已经跑过的东西。
    assert service.node_stash_carrier(NODE_ID).get(tool_arg_stash.STASH_CONTAINER_KEY, {}).get(SCOPE) in (None, [])


@pytest.mark.asyncio
async def test_plain_call_is_unaffected_by_the_reuse_branch(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    tool = _BatchTool()
    runtime_context = _context(
        carrier=service.persist_node_stash_carrier(NODE_ID, _carrier(hop=4, staged=[{"value": "staged"}])),
        contract_present=True,
    )

    await _run(loop, tool, {"value": "direct"}, runtime_context)

    assert tool.seen == ["direct"]


@pytest.mark.asyncio
async def test_reuse_for_a_name_this_hop_cannot_call_executes_nothing(tmp_path: Path) -> None:
    """权限这一跳已收走（名字不在本轮实例表里）⇒ 整批交回普通路径，一份都不撞。

    复用分支拿的是"上一跳已校验过"的入参，执行侧仍要按当轮重新认定可得性/RBAC（D7-3）。
    不交回而是逐份执行的话，模型会看到 N 条同样的拒绝；把它接进复用分支自己处理又会造出
    第二条没有名单的回执。暂存不清空：严格一跳的判据自己会把它作废。
    """
    service = _service(tmp_path)
    loop = _loop(service)
    executed: list[dict] = []

    async def _spy(**kwargs):
        executed.append(kwargs)
        raise AssertionError("reuse must not reach execution for a name this hop cannot call")

    loop._execute_tool = _spy
    carrier = service.persist_node_stash_carrier(NODE_ID, _carrier(hop=4, staged=[{"value": "a"}, {"value": "b"}]))
    runtime_context = _context(carrier=carrier, contract_present=True)
    runtime_context["candidate_tool_names"] = [TOOL_ID]

    content = await loop._execute_tool_raw(
        tools={},
        tool_name=TOOL_ID,
        arguments={"reuse_args": True},
        runtime_context=runtime_context,
    )

    assert executed == []
    assert "tool not available" in str(content)
    assert TOOL_ID in str(content)
    container = service.node_stash_carrier(NODE_ID).get(tool_arg_stash.STASH_CONTAINER_KEY, {})
    assert len(container.get(SCOPE) or []) == 2


# ---------------------------------------------------------------------------
# 计划器的四类答案（互斥性只在纯函数里能一次看全）
# ---------------------------------------------------------------------------


def test_planner_kinds_are_exclusive() -> None:
    staged = [{"args": {"value": "a"}, "args_digest": "d1"}]
    assert plan_reuse_execution(
        reuse_requested=False,
        explicit_arguments={"value": "a"},
        contract_present=True,
        staged_entries=staged,
    )["kind"] == ""
    explicit = plan_reuse_execution(
        reuse_requested=True,
        explicit_arguments={"value": "a"},
        contract_present=False,
        staged_entries=staged,
    )
    assert explicit["kind"] == REUSE_PLAN_EXPLICIT_ARGS
    assert plan_reuse_execution(
        reuse_requested=True,
        explicit_arguments={},
        contract_present=False,
        staged_entries=staged,
    )["kind"] == REUSE_PLAN_CONTRACT_ABSENT
    assert plan_reuse_execution(
        reuse_requested=True,
        explicit_arguments={},
        contract_present=True,
        staged_entries=[],
    )["kind"] == REUSE_PLAN_NO_STASH
    assert plan_reuse_execution(
        reuse_requested=True,
        explicit_arguments={},
        contract_present=True,
        staged_entries=staged,
    )["kind"] == REUSE_PLAN_EXECUTE


def test_reuse_flag_never_reaches_the_tool_arguments() -> None:
    """`reuse_args` 是传输开关，不是业务入参：两份车道拿到的入参都必须没有它。"""
    reuse, remaining = split_reuse_arguments({"reuse_args": "true", "value": "a"})
    assert reuse is True
    assert remaining == {"value": "a"}

    off, kept = split_reuse_arguments({"reuse_args": False, "value": "a"})
    assert off is False
    assert kept == {"value": "a"}


# ---------------------------------------------------------------------------
# D11：尾块字段
# ---------------------------------------------------------------------------


def test_contract_blocks_drop_callable_tools_and_keep_denied_tools() -> None:
    frontdoor_contract = _render_frontdoor_contract_summary(
        {
            "callable_tool_names": ["exec", "filesystem_edit"],
            "denied_tool_names": ["perf_inspect"],
        }
    )
    node_contract = _render_node_dynamic_contract_summary(
        {
            "callable_tool_names": ["exec", "filesystem_edit"],
            "denied_tool_names": ["perf_inspect"],
        }
    )
    for text in (frontdoor_contract, node_contract):
        assert "callable_tools:" not in text
        assert "denied_tools" in text
        assert "perf_inspect" in text


def _presence_message(tool_id: str = TOOL_ID, fingerprint: str = FINGERPRINT) -> dict:
    return {
        "role": "tool",
        "name": "load_tool_context",
        "tool_call_id": "call-load-1",
        "content": json.dumps(
            {
                "ok": True,
                "tool_id": tool_id,
                "tool_context_fingerprint": fingerprint,
                "content": render_tool_contract_block(tool_id, fingerprint, "# filesystem_write\n契约正文"),
            },
            ensure_ascii=False,
        ),
    }


@pytest.mark.asyncio
async def test_frontdoor_reuse_aggregates_through_the_same_planner() -> None:
    """前门那条复用路径与节点道同判定：同一份 plan、同一套回执与聚合形状。"""
    session_scope = tool_arg_stash.session_scope_key("web:shared", TOOL_ID)
    carrier = _carrier(hop=3, staged=[{"value": "a"}, {"value": "b"}], scope_key=session_scope)
    session = SimpleNamespace(session_key="web:shared", _frontdoor_tool_arg_stash=carrier)
    runtime_context = {
        "session_key": "web:shared",
        "runtime_session": session,
        "tool_arg_stash_carrier": carrier,
    }
    seen: list[str] = []

    async def _execute_one(arguments: dict) -> tuple[str, str]:
        value = str((arguments or {}).get("value"))
        seen.append(value)
        return json.dumps({"ok": True, "value": value}, ensure_ascii=False), "success"

    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    content = await ops._frontdoor_reuse_args_execution(
        tool_name=TOOL_ID,
        arguments={"reuse_args": True},
        state={"messages": [_presence_message()]},
        runtime_context=runtime_context,
        execute_one=_execute_one,
        call_id="call-fd-1",
    )

    payload = json.loads(str(content or "{}"))
    assert payload["ok"] is True
    assert payload["executed"] == 2
    assert payload["failed"] == 0
    assert seen == ["a", "b"]
    assert [item["ok"] for item in payload["items"]] == [True, True]
    # 道按 call id 记一次，供阶段记录行落盘；下游不必用回执文本反推。
    assert runtime_context["tool_execution_paths"] == {"call-fd-1": "reused"}


@pytest.mark.asyncio
async def test_frontdoor_plain_recall_after_staging_is_marked_corrected() -> None:
    """上一跳暂存过、这一跳带**同一份**入参直接调 ⇒ `corrected` + `staged_replay=identical`。

    这一格存在的唯一理由是：`corrected` 一个词把"该用 reuse 没用"和"本来就该换参数重发"
    混成了同一个数，而基线要数的正是前者。
    """
    session_scope = tool_arg_stash.session_scope_key("web:shared", TOOL_ID)
    carrier = _carrier(hop=3, staged=[{"value": "a"}], scope_key=session_scope)
    session = SimpleNamespace(session_key="web:shared", _frontdoor_tool_arg_stash=carrier)
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)

    async def _unused(_arguments: dict) -> tuple[str, str]:  # pragma: no cover
        raise AssertionError("plain call must not execute through the reuse branch")

    runtime_context = {
        "session_key": "web:shared",
        "runtime_session": session,
        "tool_arg_stash_carrier": carrier,
    }
    content = await ops._frontdoor_reuse_args_execution(
        tool_name=TOOL_ID,
        arguments={"value": "a"},
        state={"messages": [_presence_message()]},
        runtime_context=runtime_context,
        execute_one=_unused,
        call_id="call-fd-2",
    )
    assert content is None
    assert runtime_context["tool_execution_paths"] == {"call-fd-2": "corrected"}
    assert runtime_context["tool_staged_replay"] == {"call-fd-2": "identical"}

    other_context = {
        "session_key": "web:shared",
        "runtime_session": session,
        "tool_arg_stash_carrier": carrier,
    }
    await ops._frontdoor_reuse_args_execution(
        tool_name=TOOL_ID,
        arguments={"value": "different"},
        state={"messages": [_presence_message()]},
        runtime_context=other_context,
        execute_one=_unused,
        call_id="call-fd-3",
    )
    assert other_context["tool_execution_paths"] == {"call-fd-3": "corrected"}
    assert other_context["tool_staged_replay"] == {"call-fd-3": "new_args"}


@pytest.mark.asyncio
async def test_frontdoor_reuse_flag_with_args_is_counted_apart() -> None:
    """打 `reuse_args` 又给显式入参 ⇒ 标记被忽略，这一档要和"没打标记"分开数。

    实盘见过模型在思考里写明"用 reuse_args=true 重跑"，落到调用体却是整套参数——因为
    schema 里业务参数照旧必填。两种 `corrected` 要修的地方完全不同，混成一个数就只能猜。
    """
    session_scope = tool_arg_stash.session_scope_key("web:shared", TOOL_ID)
    carrier = _carrier(hop=3, staged=[{"value": "a"}], scope_key=session_scope)
    session = SimpleNamespace(session_key="web:shared", _frontdoor_tool_arg_stash=carrier)
    runtime_context = {
        "session_key": "web:shared",
        "runtime_session": session,
        "tool_arg_stash_carrier": carrier,
    }
    seen: list[str] = []

    async def _execute_one(arguments: dict) -> tuple[str, str]:
        value = str((arguments or {}).get("value"))
        seen.append(value)
        return "ok", "success"

    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    content = await ops._frontdoor_reuse_args_execution(
        tool_name=TOOL_ID,
        arguments={"reuse_args": True, "value": "explicit"},
        state={"messages": [_presence_message()]},
        runtime_context=runtime_context,
        execute_one=_execute_one,
        call_id="call-fd-6",
    )

    assert str(content or "").startswith("reuse_args ignored")
    assert seen == ["explicit"]
    assert runtime_context["tool_execution_paths"] == {"call-fd-6": "corrected"}
    assert runtime_context["tool_reuse_flag_ignored"] == {"call-fd-6": True}
    assert runtime_context["tool_staged_replay"] == {"call-fd-6": "new_args"}


@pytest.mark.asyncio
async def test_frontdoor_plain_call_without_staging_records_nothing() -> None:
    """容器里没有这个名字的条目时，普通调用一个键都不写，也不摊在场视图。"""
    session_scope = tool_arg_stash.session_scope_key("web:shared", TOOL_ID)
    empty_carrier = _carrier(hop=3, staged=[], scope_key=session_scope)
    session = SimpleNamespace(session_key="web:shared", _frontdoor_tool_arg_stash=empty_carrier)
    runtime_context: dict = {
        "session_key": "web:shared",
        "runtime_session": session,
        "tool_arg_stash_carrier": empty_carrier,
    }
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)

    async def _unused(_arguments: dict) -> tuple[str, str]:  # pragma: no cover
        raise AssertionError("plain call must not execute through the reuse branch")

    content = await ops._frontdoor_reuse_args_execution(
        tool_name=TOOL_ID,
        arguments={"value": "a"},
        state=None,
        runtime_context=runtime_context,
        execute_one=_unused,
        call_id="call-fd-4",
    )
    assert content is None
    assert "tool_execution_paths" not in runtime_context
    assert "tool_staged_replay" not in runtime_context


@pytest.mark.asyncio
async def test_frontdoor_reuse_item_status_comes_from_execution() -> None:
    """外置后的失败信封不以 Error 开头；逐项 ok 必须取执行侧状态。"""
    session_scope = tool_arg_stash.session_scope_key("web:shared", TOOL_ID)
    carrier = _carrier(hop=3, staged=[{"value": "big"}], scope_key=session_scope)
    session = SimpleNamespace(session_key="web:shared", _frontdoor_tool_arg_stash=carrier)
    runtime_context = {
        "session_key": "web:shared",
        "runtime_session": session,
        "tool_arg_stash_carrier": carrier,
    }

    async def _execute_one(arguments: dict) -> tuple[str, str]:
        _ = arguments
        return (
            "Tool call failed: tool:filesystem_write error output externalized "
            "(1 lines, 7000 chars).",
            "error",
        )

    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    content = await ops._frontdoor_reuse_args_execution(
        tool_name=TOOL_ID,
        arguments={"reuse_args": True},
        state={"messages": [_presence_message()]},
        runtime_context=runtime_context,
        execute_one=_execute_one,
        call_id="call-fd-5",
    )

    payload = json.loads(str(content or "{}"))
    assert payload["items"][0]["ok"] is False
    assert "error" in payload["items"][0]
    assert payload["failed"] == 1


def test_oversized_failure_keeps_its_failed_status(tmp_path: Path) -> None:
    """失败结果被外置成 `Tool call failed: …` 信封后，文本判据认不出失败。

    批量项不能拿渲染文本反推成败，否则一份 7 KB 的失败载荷会被报成 `ok:true`——正常路径
    也按原始结果算状态，两处必须同源。
    """
    service = _service(tmp_path)
    loop = _loop(service)
    runtime_context = _context(carrier=_carrier(hop=4, staged=[{"value": "a"}]), contract_present=True)

    text, status = loop._render_tool_message_with_status(
        {"ok": False, "error": "disk full", "padding": "x" * 7000},
        runtime_context=runtime_context,
        tool_name=TOOL_ID,
    )

    assert status == "error"
    # 旧判据在**外置信封**文本上失效——这是批量项不能拿渲染文本反推成败的原因。
    assert (
        is_error_like_tool_result(
            "Tool call failed: exec error output externalized (artifact:abc) — open with content_open(ref=...)"
        )
        is False
    )
