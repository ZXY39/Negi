"""Stage 4（一）：未水合工具被直接调用时的那一跳。

只测两条车道共用的服务层投递（`MainRuntimeService.deliver_impulse_contract`）——分支结构、
校验口径、暂存规则、回执形态都只有一份，先在这一层钉死；车道接线（节点 `_execute_tool_raw`
/ 前门执行准入）在 Stage 4（二）里按各自的进口喂。

覆盖计划 §4 Stage 4 的验证项中属于这一层的：校验通过 ⇒ 落 stash 且**未执行**；校验失败 ⇒
不落 stash；同轮第 2..N 次 ⇒ 只有第 1 份带完整块；不是"没水合"那一类 ⇒ 交回 reject 让
调用方保留原拒绝回执。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from g3ku.runtime import tool_arg_stash
from g3ku.runtime.tool_contract_block import MARKER_CLOSE, MARKER_NAME
from g3ku.runtime.tool_impulse import IMPULSE_INVALID, IMPULSE_REJECT, IMPULSE_STAGED
from main.service.runtime_service import MainRuntimeService

TOOL_ID = "filesystem_edit"
SCOPE = tool_arg_stash.node_scope_key("task-1", "node-1", TOOL_ID)

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


class _ValidatingTool:
    """按 payload 的 required 做最小校验：真实工具的 validate_params 口径。"""

    def __init__(self, *, errors: list[str] | None = None, raise_exc: bool = False) -> None:
        self._errors = errors or []
        self._raise = raise_exc

    def coerce_arguments(self, arguments):
        return dict(arguments or {}), []

    def validate_params(self, params):
        if self._raise:
            raise TypeError("schema exploded")
        return list(self._errors)


def _service(tool, tmp_path: Path, *, visible: bool = True, payload: dict | None = CONTRACT_PAYLOAD) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service._resource_manager = SimpleNamespace(get_tool=lambda name: tool)
    service._visible_tool_map = lambda **kwargs: ({TOOL_ID: tool} if visible else {})
    service.get_tool_toolskill = lambda tool_id: dict(payload) if payload else None
    return service


def _deliver(service, arguments, carrier, **kwargs):
    return service.deliver_impulse_contract(
        actor_role="execution",
        session_id="web:shared",
        tool_id=TOOL_ID,
        arguments=arguments,
        scope_key=SCOPE,
        carrier=carrier,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 校验通过：落 stash、带块、不执行
# ---------------------------------------------------------------------------


def test_valid_arguments_are_staged_and_the_contract_is_delivered(tmp_path: Path) -> None:
    service = _service(_ValidatingTool(), tmp_path)
    result = _deliver(service, {"path": "/abs/a", "new_text": "x"}, {})

    assert result["outcome"] == IMPULSE_STAGED
    assert result["executed"] is False
    assert result["staged_count"] == 1
    content = result["content"]
    # 块必须在，且带工具名与修订——判据下一跳就认这一颗头。
    assert MARKER_NAME in content and MARKER_CLOSE in content
    assert f"{TOOL_ID}:" in content
    # 份数报最终值；**不**枚举入参：模型自己那条 tool_calls 就在同一请求视图里，再抄一遍是重复。
    assert f'Staged sets for "{TOOL_ID}": 1.' in content
    # 回执必须自己说"这一跳没执行"：只写契约已加载/入参通过校验会被读成执行成功。
    assert "did not run the tool - call it again to execute it" in content
    # "下一跳"必须定义成工具结果后的下一次模型调用，而不是下一条用户消息。
    assert "The next hop is the next model call after this tool result" in content
    assert "plain-text reply" in content
    # "passed validation" 只覆盖参数形状：目标/引用要到真执行时才判，回执不能说成已校验。
    assert "resolved only when the call runs" in content
    assert "/abs/a" not in content
    assert tool_arg_stash.staged_count(tool_arg_stash.carrier_container(result["carrier"]), scope_key=SCOPE) == 1


def test_staged_entry_records_the_carrier_hop_not_a_caller_supplied_number(tmp_path: Path) -> None:
    carrier = tool_arg_stash.build_carrier({}, hop=7)
    result = _deliver(_service(_ValidatingTool(), tmp_path), {"path": "/a", "new_text": "b"}, carrier)
    entries = tool_arg_stash.carrier_container(result["carrier"])[SCOPE]
    assert entries[0]["staged_hop"] == 7
    # 下一跳（hop 8）正好可消费；再下一跳就过期。
    assert len(tool_arg_stash.peek_entries(tool_arg_stash.carrier_container(result["carrier"]), scope_key=SCOPE, current_hop=8)) == 1
    assert tool_arg_stash.peek_entries(tool_arg_stash.carrier_container(result["carrier"]), scope_key=SCOPE, current_hop=9) == []


def test_same_tool_second_call_in_the_batch_points_at_the_first_block(tmp_path: Path) -> None:
    service = _service(_ValidatingTool(), tmp_path)
    first = _deliver(service, {"path": "/a", "new_text": "1"}, {}, batch_position=1, batch_total=3)
    second = _deliver(
        service,
        {"path": "/b", "new_text": "2"},
        first["carrier"],
        batch_position=2,
        batch_total=3,
    )
    assert second["outcome"] == IMPULSE_STAGED
    # 只发一份完整块：第 2 份既没有块头也没有闭标签。
    assert MARKER_NAME not in second["content"]
    assert MARKER_CLOSE not in second["content"]
    assert "see the first result for this tool" in second["content"]
    assert "set 2 of 3" in second["content"]
    # 但两份都真的进了 stash，顺序保持。
    entries = tool_arg_stash.staged_count(tool_arg_stash.carrier_container(second["carrier"]), scope_key=SCOPE)
    assert entries == 2


def test_last_receipt_reports_the_final_count_and_the_folding(tmp_path: Path) -> None:
    """同批同签名折叠成一条 ⇒ 最后一条回执要说清"N 份 / M 次调用"。

    这是模型唯一推不出来的数：它数得出自己发了几条，但数不出哪几条会被折叠。
    """
    service = _service(_ValidatingTool(), tmp_path)
    first = _deliver(service, {"path": "/a", "new_text": "1"}, {}, batch_position=1, batch_total=2)
    second = _deliver(
        service,
        {"path": "/a", "new_text": "1"},
        first["carrier"],
        batch_position=2,
        batch_total=2,
    )

    assert second["staged_count"] == 1
    assert f'Staged sets for "{TOOL_ID}": 1 (from 2 calls).' in second["content"]
    # 第一条（非末条）不报份数：那一刻的"1 份"只是半批，报出来就是假事实。
    assert "Staged sets for" not in first["content"]


def test_a_failed_last_call_still_reports_the_batch_count(tmp_path: Path) -> None:
    """末条校验失败：它不入 stash，但整批的最终份数仍要在这一条里报出来。"""

    class _SecondCallInvalid:
        def coerce_arguments(self, arguments):
            return dict(arguments or {}), []

        def validate_params(self, params):
            return [] if params.get("new_text") == "ok" else ["missing new_text"]

    service = _service(_SecondCallInvalid(), tmp_path)
    first = _deliver(service, {"path": "/a", "new_text": "ok"}, {}, batch_position=1, batch_total=2)
    second = _deliver(
        service,
        {"path": "/b"},
        first["carrier"],
        batch_position=2,
        batch_total=2,
    )

    assert second["outcome"] == IMPULSE_INVALID
    assert "failed validation and were not staged (set 2 of 2)" in second["content"]
    assert f'Staged sets for "{TOOL_ID}": 1 (from 2 calls).' in second["content"]


def test_count_excludes_entries_that_will_never_be_consumed(tmp_path: Path) -> None:
    """容器里可能还躺着上一跳过期或别的修订的条目——份数只数下一跳真会跑的那些。"""
    service = _service(_ValidatingTool(), tmp_path)
    seeded = _deliver(service, {"path": "/old", "new_text": "x"}, {}, )
    stale_container = tool_arg_stash.carrier_container(seeded["carrier"])
    carrier = tool_arg_stash.build_carrier(stale_container, hop=5)

    result = _deliver(service, {"path": "/new", "new_text": "y"}, carrier)

    assert result["staged_count"] == 1
    assert f'Staged sets for "{TOOL_ID}": 1.' in result["content"]
    # 容器里确实还是两条，只是旧那条下一跳不可消费（严格一跳）。
    assert tool_arg_stash.staged_count(tool_arg_stash.carrier_container(result["carrier"]), scope_key=SCOPE) == 2


# ---------------------------------------------------------------------------
# 校验失败：不落 stash、占一轮、仍交契约
# ---------------------------------------------------------------------------


def test_invalid_arguments_deliver_the_contract_without_staging(tmp_path: Path) -> None:
    service = _service(_ValidatingTool(errors=["missing required field: path"]), tmp_path)
    result = _deliver(service, {"new_text": "x"}, {})

    assert result["outcome"] == IMPULSE_INVALID
    assert result["staged_count"] == 0
    assert "failed validation" in result["content"]
    assert "Nothing was staged" in result["content"]
    assert MARKER_NAME in result["content"]  # 契约照样给了
    assert tool_arg_stash.carrier_container(result["carrier"]) == {}


def test_validator_exception_is_reported_as_an_invalid_argument_call(tmp_path: Path) -> None:
    result = _deliver(_service(_ValidatingTool(raise_exc=True), tmp_path), {"path": "/a"}, {})
    assert result["outcome"] == IMPULSE_INVALID
    assert tool_arg_stash.carrier_container(result["carrier"]) == {}


# ---------------------------------------------------------------------------
# reject：不是"没水合"这一类，交回调用方原有的拒绝回执
# ---------------------------------------------------------------------------


def test_rbac_invisible_tool_is_rejected_not_staged(tmp_path: Path) -> None:
    result = _deliver(_service(_ValidatingTool(), tmp_path, visible=False), {"path": "/a"}, {})
    assert result["outcome"] == IMPULSE_REJECT
    assert result["content"] == ""


def test_repair_required_tool_is_rejected(tmp_path: Path) -> None:
    payload = {**CONTRACT_PAYLOAD, "repair_required": True, "errors": ["import failed"]}
    result = _deliver(_service(_ValidatingTool(), tmp_path, payload=payload), {"path": "/a"}, {})
    assert result["outcome"] == IMPULSE_REJECT


def test_unknown_name_is_rejected(tmp_path: Path) -> None:
    result = _service(_ValidatingTool(), tmp_path).deliver_impulse_contract(
        actor_role="execution",
        session_id="web:shared",
        tool_id="not_a_real_tool",
        arguments={},
        scope_key=tool_arg_stash.node_scope_key("task-1", "node-1", "not_a_real_tool"),
        carrier={},
    )
    assert result["outcome"] == IMPULSE_REJECT
