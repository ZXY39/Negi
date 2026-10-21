"""Stage 4（三）：前门道未水合调用的冲动投递。

测的是前门执行准入上的那一支（`_frontdoor_impulse_contract_content`）：它拿的是**请求视图**
（与 callable 收窄同一个判据函数）、写的是**会话载体的那个属性**（`reuse_args` 下一跳读的
就是它）。两者任一处接错，回执就会自说自话而 stash 是空的。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from g3ku.runtime import tool_arg_stash
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.tool_contract_block import MARKER_NAME
from g3ku.runtime.tool_impulse import IMPULSE_INVALID, IMPULSE_REJECT, IMPULSE_STAGED
from main.models import NodeRecord
from main.service.runtime_service import MainRuntimeService

TOOL_ID = "filesystem_edit"
SESSION_KEY = "web:shared"
SCOPE = tool_arg_stash.session_scope_key(SESSION_KEY, TOOL_ID)

CONTRACT_PAYLOAD = {
    "tool_id": TOOL_ID,
    "content": "# filesystem_edit\n\nEdit a file in place.\n",
    "parameter_contract_markdown": "## Parameter Contract\n",
    "parameters_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "new_text": {"type": "string"}},
        "required": ["path", "new_text"],
    },
    "required_parameters": ["path", "new_text"],
    "example_arguments": {},
    "warnings": [],
    "errors": [],
    "callable": True,
    "available": True,
    "repair_required": False,
}


def _service(tmp_path: Path, *, validation_errors: list[str] | None = None) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )

    class _Instance:
        def validate_params(self, params):
            return list(validation_errors or [])

    service._resource_manager = SimpleNamespace(get_tool=lambda name: _Instance())
    service._visible_tool_map = lambda **kwargs: {TOOL_ID: object()}
    service.get_tool_toolskill = lambda tool_id: dict(CONTRACT_PAYLOAD)
    service.store.upsert_node(
        NodeRecord(
            node_id="node-impulse",
            task_id="task-impulse",
            root_node_id="node-impulse",
            goal="g",
            prompt="p",
            created_at="2026-10-20T20:00:00+08:00",
            updated_at="2026-10-20T20:00:00+08:00",
        )
    )
    return service


def _ops(service: MainRuntimeService) -> CeoFrontDoorRuntimeOps:
    # 既有测试的同一构造法：判据只吃 `_loop` 上的两个句柄，绕开整张图的装配。
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(
        main_task_service=service,
        tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
        sessions=SimpleNamespace(get_or_create=lambda key: None),
        app_config=None,
    )
    return ops


def _session() -> SimpleNamespace:
    return SimpleNamespace(session_key=SESSION_KEY, _frontdoor_tool_arg_stash={})


def _runtime_context(session) -> dict:
    return {
        "session_key": SESSION_KEY,
        "actor_role": "ceo",
        "runtime_session": session,
        "impulse_delivered_tools": {},
        "impulse_batch_totals": {TOOL_ID: 2},
        "tool_arg_stash_carrier": _advance(session),
    }


def _advance(session) -> dict:
    return tool_arg_stash.build_carrier({}, hop=1)


def _call(ops, *, tool_name, arguments, state, runtime_context):
    return ops._frontdoor_impulse_contract_content(
        tool_name=tool_name,
        arguments=arguments,
        state=state,
        runtime_context=runtime_context,
    )


# ---------------------------------------------------------------------------


def test_impulsive_call_stages_into_the_session_carrier(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ops = _ops(service)
    session = _session()
    context = _runtime_context(session)
    state = {"messages": [{"role": "user", "content": "go"}]}

    outcome = _call(ops, tool_name=TOOL_ID, arguments={"path": "/abs/a", "new_text": "x"}, state=state, runtime_context=context)

    assert outcome is not None
    kind, content = outcome
    assert kind == IMPULSE_STAGED
    assert MARKER_NAME in content
    assert "passed validation and were staged" in content
    # 这份上下文模拟"同批两条调用"，本条是第一条：份数只在末条报（那一刻才看得见整批）。
    assert "Staged sets for" not in content
    # 关键：回执说"已暂存"，stash 里就必须真的有——模型下一跳读的是会话那个属性。
    stored_container = tool_arg_stash.carrier_container(getattr(session, "_frontdoor_tool_arg_stash"))
    assert tool_arg_stash.staged_count(stored_container, scope_key=SCOPE) == 1
    assert stored_container[SCOPE][0]["args"] == {"path": "/abs/a", "new_text": "x"}


def test_contract_already_in_the_request_view_is_not_impulsive(tmp_path: Path) -> None:
    """请求视图里已有该工具的块 ⇒ 走正常执行，不该被降级成"只交契约"。"""
    from g3ku.runtime.tool_contract_block import render_tool_contract_block

    service = _service(tmp_path)
    ops = _ops(service)
    session = _session()
    context = _runtime_context(session)
    state = {
        "messages": [
            {
                "role": "tool",
                "name": "load_tool_context",
                "content": json.dumps(
                    {
                        "ok": True,
                        "tool_id": TOOL_ID,
                        "content": render_tool_contract_block(TOOL_ID, "tcf:live", "# filesystem_edit"),
                    },
                    ensure_ascii=False,
                ),
            }
        ]
    }

    assert _call(ops, tool_name=TOOL_ID, arguments={"path": "/a"}, state=state, runtime_context=context) is None


def test_invalid_arguments_return_the_contract_without_staging(tmp_path: Path) -> None:
    service = _service(tmp_path, validation_errors=["missing required field: new_text"])
    ops = _ops(service)
    session = _session()
    context = _runtime_context(session)
    state = {"messages": []}

    kind, content = _call(ops, tool_name=TOOL_ID, arguments={"path": "/a"}, state=state, runtime_context=context)

    assert kind == IMPULSE_INVALID
    assert "Nothing was staged" in content
    assert tool_arg_stash.carrier_container(getattr(session, "_frontdoor_tool_arg_stash")) == {}


def test_fixed_builtin_and_rejected_names_are_not_treated_as_impulsive(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ops = _ops(service)
    session = _session()
    state = {"messages": []}

    assert _call(ops, tool_name="exec", arguments={}, state=state, runtime_context=_runtime_context(session)) is None
    assert (
        _call(
            ops,
            tool_name="not_visible_at_all",
            arguments={},
            state=state,
            runtime_context=_runtime_context(session),
        )
        is None
    )
    assert _call(ops, tool_name=TOOL_ID, arguments={}, state=state, runtime_context={}) is None


def test_delivery_failure_does_not_let_the_call_execute(tmp_path: Path) -> None:
    """投递本身抛异常 ⇒ 交回错误，**不放行执行**（未水合还让它跑过去就破掉不变量）。"""
    service = _service(tmp_path)
    service.deliver_impulse_contract = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("resource manager gone"))
    ops = _ops(service)
    session = _session()

    kind, content = _call(
        ops,
        tool_name=TOOL_ID,
        arguments={"path": "/a"},
        state={"messages": []},
        runtime_context=_runtime_context(session),
    )
    assert kind == "failed"
    assert "could not deliver the contract" in content


def test_second_call_in_the_batch_carries_no_second_block(tmp_path: Path) -> None:
    service = _service(tmp_path)
    ops = _ops(service)
    session = _session()
    context = _runtime_context(session)
    state = {"messages": []}

    _call(ops, tool_name=TOOL_ID, arguments={"path": "/a", "new_text": "1"}, state=state, runtime_context=context)
    context["current_tool_call_id"] = "call-2"
    _kind, second = _call(
        ops,
        tool_name=TOOL_ID,
        arguments={"path": "/b", "new_text": "2"},
        state=state,
        runtime_context=context,
    )
    assert MARKER_NAME not in second
    assert "set 2 of 2" in second
    container = tool_arg_stash.carrier_container(getattr(session, "_frontdoor_tool_arg_stash"))
    assert [entry["args"]["path"] for entry in container[SCOPE]] == ["/a", "/b"]


def test_reject_outcome_falls_through_to_the_existing_refusal(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.deliver_impulse_contract = lambda **kwargs: {
        "outcome": IMPULSE_REJECT,
        "content": "",
        "carrier": {},
        "staged_count": 0,
    }
    ops = _ops(service)
    assert (
        _call(
            ops,
            tool_name=TOOL_ID,
            arguments={"path": "/a"},
            state={"messages": []},
            runtime_context=_runtime_context(_session()),
        )
        is None
    )


def test_round_tool_entry_carries_the_contract_delivery_marker(tmp_path: Path) -> None:
    """阶段账本要带上"只交契约、没执行"这颗标记，Web 轨道才能把行画成"加载 tool"。"""
    ops = _ops(_service(tmp_path))
    payload = {"id": "call-1", "name": TOOL_ID, "arguments": {"path": "/a", "new_text": "x"}}

    delivered = ops._frontdoor_round_tool_entry(
        payload=payload,
        result={
            "tool_call_id": "call-1",
            "tool_name": TOOL_ID,
            "status": "success",
            "result_text": "[TOOL_CONTRACT_V1:filesystem_edit:tcf:abc]\nbody\n[/TOOL_CONTRACT_V1]",
            "contract_delivery": IMPULSE_STAGED,
            "tool_message": {"started_at": "", "finished_at": "", "elapsed_seconds": None},
        },
        source="user",
    )
    assert delivered["contract_delivery"] == IMPULSE_STAGED

    executed = ops._frontdoor_round_tool_entry(
        payload=payload,
        result={"tool_call_id": "call-1", "tool_name": TOOL_ID, "status": "success", "result_text": "ok"},
        source="user",
    )
    assert "contract_delivery" not in executed


def test_round_tool_entry_carries_the_impulse_ledger_marks(tmp_path: Path) -> None:
    """`execution_path` 与 `staged_replay` 要随执行行进阶段账本。

    前门没有节点那种按 call 落库的审计行：这两格不在这里落，reuse 缺口就只能靠手翻
    请求 artifact 数——那既不是耐久读数，也挡不住回执文本反推那一类误判。
    """
    ops = _ops(_service(tmp_path))
    payload = {"id": "call-1", "name": TOOL_ID, "arguments": {"reuse_args": True}}

    reused = ops._frontdoor_round_tool_entry(
        payload=payload,
        result={
            "tool_call_id": "call-1",
            "tool_name": TOOL_ID,
            "status": "success",
            "result_text": '{"ok": true, "executed": 2, "failed": 0}',
            "execution_path": "reused",
            "tool_message": {"started_at": "", "finished_at": "", "elapsed_seconds": None},
        },
        source="user",
    )
    assert reused["execution_path"] == "reused"
    assert "staged_replay" not in reused

    replayed = ops._frontdoor_round_tool_entry(
        payload=payload,
        result={
            "tool_call_id": "call-1",
            "tool_name": TOOL_ID,
            "status": "success",
            "result_text": "ok",
            "execution_path": "corrected",
            "staged_replay": "identical",
        },
        source="user",
    )
    assert replayed["execution_path"] == "corrected"
    assert replayed["staged_replay"] == "identical"


def test_reuse_branch_requires_a_call_id(tmp_path: Path) -> None:
    """`call_id` 是必填参数：道按 call 记，调用点漏传就会静默退化成 normal。

    实盘第一次就是因为前门调用点没传它，耐久行里只有 normal、一条 reused 都没有。
    """
    import inspect

    from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps

    parameters = inspect.signature(CeoFrontDoorRuntimeOps._frontdoor_reuse_args_execution).parameters
    assert parameters["call_id"].default is inspect.Parameter.empty
