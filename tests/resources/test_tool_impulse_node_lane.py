"""Stage 4（二）：节点道执行准入上的冲动调用投递。

用例从车道的进口喂（`ReActToolLoop._execute_tool_raw`），不直接喂渲染函数：
执行准入、闸门豁免、同轮单发、暂存落点这几件事只有在这个入口上才同时成立。

覆盖计划 §4 Stage 4 的验证项：

- 未水合工具直接调用 ⇒ 返回契约块 + 入 stash + **工具没被执行**（handler 从未被调用）；
- 入参非法 ⇒ 契约 + 参数错误 + **未** stash；
- 同轮 3 次冲动调用同工具 ⇒ 只有第 1 份带完整块；
- 无活动阶段时冲动调用**成功**（不被阶段闸门拦下）；
- 熔断/重复调用判据把未执行的冲动调用排除在外。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from g3ku.agent.tools.base import Tool
from g3ku.runtime import tool_arg_stash
from g3ku.runtime.tool_contract_block import MARKER_CLOSE, MARKER_NAME
from main.models import NodeRecord
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService

TOOL_ID = "filesystem_edit"
TASK_ID = "task-impulse"
NODE_ID = "node-impulse"
SCOPE = tool_arg_stash.node_scope_key(TASK_ID, NODE_ID, TOOL_ID)

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


class _ExplodingTool(Tool):
    """被调用就抛：用它证明冲动路径**没有**执行工具。"""

    calls = 0

    @property
    def name(self) -> str:
        return TOOL_ID

    @property
    def description(self) -> str:
        return "edit"

    @property
    def parameters(self) -> dict:
        return dict(CONTRACT_PAYLOAD["parameters_schema"])

    async def execute(self, **kwargs):
        type(self).calls += 1
        raise AssertionError("the tool must not execute on an impulsive call")


class _RecordingTool(_ExplodingTool):
    """契约已在场时该走正常执行：这条工具记下"确实跑了"。"""

    calls = 0

    async def execute(self, **kwargs):
        type(self).calls += 1
        return "executed"


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
    # 冲动路径把暂存写回 `node.metadata`，所以这条节点行必须真的在库里——没有它，
    # 用例会退化成"只测内存算术"，pause/resume 存活那条承诺就没人钉。
    service.store.upsert_node(
        NodeRecord(
            node_id=NODE_ID,
            task_id=TASK_ID,
            root_node_id=NODE_ID,
            goal="g",
            prompt="p",
            created_at="2026-10-20T20:00:00+08:00",
            updated_at="2026-10-20T20:00:00+08:00",
        )
    )
    return service


def _loop(service: MainRuntimeService) -> ReActToolLoop:
    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=SimpleNamespace(), max_iterations=2)
    loop._tool_impulse_service = service
    return loop


def _context(*, presence: dict | None = None, carrier: dict | None = None, batch_totals: dict | None = None) -> dict:
    return {
        'task_id': TASK_ID,
        'node_id': NODE_ID,
        'actor_role': 'execution',
        'session_key': 'web:shared',
        'tool_contract_presence': {} if presence is None else presence,
        'tool_arg_stash_carrier': carrier or {},
        'impulse_delivered_tools': {},
        'impulse_staged_call_ids': set(),
        'impulse_batch_totals': batch_totals or {TOOL_ID: 1},
        'tool_contract_deliveries': {},
        'current_tool_call_id': 'call-1',
        # 没开阶段：闸门今天会先把非豁免工具的调用拒掉。
        'stage_turn_granted': False,
    }


async def _run(loop: ReActToolLoop, tools: dict, arguments: dict, context: dict) -> str:
    return await loop._execute_tool_raw(
        tools=tools,
        tool_name=TOOL_ID,
        arguments=arguments,
        runtime_context=context,
    )


# ---------------------------------------------------------------------------
# 校验通过：交契约、入 stash、不执行
# ---------------------------------------------------------------------------


async def test_impulsive_call_returns_contract_and_stages_without_executing(tmp_path: Path) -> None:
    _ExplodingTool.calls = 0
    service = _service(tmp_path)
    loop = _loop(service)
    context = _context()

    content = await _run(loop, {TOOL_ID: _ExplodingTool()}, {"path": "/abs/a", "new_text": "x"}, context)

    assert _ExplodingTool.calls == 0
    assert MARKER_NAME in content and MARKER_CLOSE in content
    # 回执只报份数；"怎么复用"住在提示词规则里，不在每次投递时复读。
    assert f'Staged sets for "{TOOL_ID}": 1.' in content
    # 暂存真进了载体，并且下一跳可消费。
    carrier = context["tool_arg_stash_carrier"]
    container = tool_arg_stash.carrier_container(carrier)
    assert tool_arg_stash.staged_count(container, scope_key=SCOPE) == 1
    assert len(tool_arg_stash.peek_entries(container, scope_key=SCOPE, current_hop=1)) == 1
    assert context["impulse_staged_call_ids"] == {"call-1"}
    # Web 面靠这颗按调用行落盘的标记把该行画成"加载 tool"，而不是一次普通工具调用。
    assert context["tool_contract_deliveries"] == {"call-1": "staged"}


async def test_tool_already_visible_in_context_executes_normally(tmp_path: Path) -> None:
    """契约已在场 ⇒ 这不是冲动调用，准入面照旧放行执行。"""
    service = _service(tmp_path)
    loop = _loop(service)
    context = _context(presence={TOOL_ID: "tcf:present-revision"})
    # 这一条测的是"契约已在场 ⇒ 照旧执行"，所以要把阶段闸门放在它背后：闸门对非豁免工具
    # 的拦截是另一件事（另一条用例专门测冲动调用排在闸门之前）。
    context["stage_turn_granted"] = True
    _RecordingTool.calls = 0
    tool = _RecordingTool()

    content = await _run(loop, {TOOL_ID: tool}, {"path": "/abs/a", "new_text": "x"}, context)

    assert _RecordingTool.calls == 1
    assert content == "executed"
    assert tool_arg_stash.staged_count(
        tool_arg_stash.carrier_container(context["tool_arg_stash_carrier"]), scope_key=SCOPE
    ) == 0


async def test_fixed_builtin_is_never_treated_as_impulsive(tmp_path: Path) -> None:
    """常驻内置没有 toolskill，不能被判成"契约不在场"而锁死（D13 同源的那类事故）。"""
    service = _service(tmp_path)
    loop = _loop(service)
    context = _context()
    context['current_tool_call_id'] = 'call-silent'

    content = await loop._execute_tool_raw(
        tools={'filesystem_stat': object()},
        tool_name='filesystem_stat',
        arguments={},
        runtime_context=context,
    )
    # 走到执行路径才会有的错误不是契约回执。
    assert MARKER_NAME not in str(content or "")


# ---------------------------------------------------------------------------
# 校验失败：契约给、参数错误给、不入 stash
# ---------------------------------------------------------------------------


async def test_invalid_arguments_stay_out_of_the_stash(tmp_path: Path) -> None:
    service = _service(tmp_path, validation_errors=["missing required field: new_text"])
    loop = _loop(service)
    context = _context()

    content = await _run(loop, {TOOL_ID: _ExplodingTool()}, {"path": "/abs/a"}, context)

    assert "failed validation" in content
    assert "Nothing was staged" in content
    assert MARKER_NAME in content
    assert tool_arg_stash.staged_count(
        tool_arg_stash.carrier_container(context["tool_arg_stash_carrier"]), scope_key=SCOPE
    ) == 0
    # 校验失败占一轮预算（不免计，否则模型零成本换着错法重试）。
    assert context["impulse_staged_call_ids"] == set()
    # 参数错了也还是"只交契约"的一行；卡片族仍按加载 tool 画。
    assert context["tool_contract_deliveries"] == {"call-1": "invalid"}


# ---------------------------------------------------------------------------
# 同轮多发：只第一份带块；无阶段也放行
# ---------------------------------------------------------------------------


async def test_only_the_first_receipt_in_a_batch_carries_the_block(tmp_path: Path) -> None:
    service = _service(tmp_path)
    loop = _loop(service)
    context = _context(batch_totals={TOOL_ID: 3})

    first = await _run(loop, {TOOL_ID: _ExplodingTool()}, {"path": "/a", "new_text": "1"}, context)
    context["current_tool_call_id"] = "call-2"
    second = await _run(loop, {TOOL_ID: _ExplodingTool()}, {"path": "/b", "new_text": "2"}, context)
    context["current_tool_call_id"] = "call-3"
    third = await _run(loop, {TOOL_ID: _ExplodingTool()}, {"path": "/c", "new_text": "3"}, context)

    assert MARKER_NAME in first and MARKER_CLOSE in first
    assert MARKER_NAME not in second and MARKER_CLOSE not in second
    assert MARKER_NAME not in third
    assert "set 2 of 3" in second and "set 3 of 3" in third
    container = tool_arg_stash.carrier_container(context["tool_arg_stash_carrier"])
    paths = [entry["args"]["path"] for entry in container[SCOPE]]
    assert paths == ["/a", "/b", "/c"]


async def test_impulse_bypasses_the_stage_gate_with_no_active_stage(tmp_path: Path) -> None:
    """没有活动阶段时，冲动调用仍然成功（它排在闸门之前，且不谎报"需先开阶段"）。"""
    service = _service(tmp_path)
    loop = _loop(service)
    context = _context()
    context["stage_gate"] = {"enabled": True, "has_active_stage": False, "transition_required": False}

    content = await _run(loop, {TOOL_ID: _ExplodingTool()}, {"path": "/a", "new_text": "b"}, context)

    assert "需先开阶段" not in content
    assert "Error: " not in content
    assert f'Staged sets for "{TOOL_ID}": 1.' in content


# ---------------------------------------------------------------------------
# 熔断判据：未执行的冲动调用不进签名序列
# ---------------------------------------------------------------------------


def test_breaker_predicate_excludes_impulse_applicable_names(tmp_path: Path) -> None:
    loop = _loop(_service(tmp_path))
    assert loop._impulse_contract_applicable(tool_name=TOOL_ID, runtime_context=_context()) is True
    assert (
        loop._impulse_contract_applicable(
            tool_name=TOOL_ID,
            runtime_context=_context(presence={TOOL_ID: "tcf:x"}),
        )
        is False
    )
    # 常驻内置永不冲动（判据视图为空也不拦它）。
    assert loop._impulse_contract_applicable(tool_name="exec", runtime_context=_context()) is False
    # 循环注入的控制信号也没有契约可交：把它们算成冲动调用，等于让重复调用熔断
    # 对 `submit_message_distribution` / `submit_notice_inspection_decision` 失明。
    for control_name in ("submit_message_distribution", "submit_notice_inspection_decision", "silent"):
        assert loop._impulse_contract_applicable(tool_name=control_name, runtime_context=_context()) is False
    # 前门的常驻内置在节点道是带 toolskill 的资源工具，必须仍能被冲动投递救回来。
    assert loop._impulse_contract_applicable(tool_name="memory_note", runtime_context=_context()) is True
    # 判据视图缺失（旁路调用）时不走冲动路径，也不该被熔断豁免误伤。
    context = _context()
    context.pop("tool_contract_presence")
    assert loop._impulse_contract_applicable(tool_name=TOOL_ID, runtime_context=context) is False


async def test_identical_repeated_impulse_stages_once(tmp_path: Path) -> None:
    """同签名的重复调用只入一条：否则 reuse_args 会把同一份入参执行两遍。"""
    service = _service(tmp_path)
    loop = _loop(service)
    context = _context(batch_totals={TOOL_ID: 2})

    await _run(loop, {TOOL_ID: _ExplodingTool()}, {"path": "/a", "new_text": "same"}, context)
    context["current_tool_call_id"] = "call-2"
    await _run(loop, {TOOL_ID: _ExplodingTool()}, {"path": "/a", "new_text": "same"}, context)

    container = tool_arg_stash.carrier_container(context["tool_arg_stash_carrier"])
    assert tool_arg_stash.staged_count(container, scope_key=SCOPE) == 1


def test_execution_trace_keeps_the_delivery_marker() -> None:
    """展示标记必须穿过投影行落盘与轨迹重建，否则节点详情画不出"加载 tool"卡片。"""
    from main.models import NodeOutputEntry
    from main.monitoring.execution_trace import build_execution_trace
    from main.monitoring.models import TaskProjectionToolResultRecord
    from main.protocol import now_iso

    node = NodeRecord(
        node_id=NODE_ID,
        task_id=TASK_ID,
        root_node_id=NODE_ID,
        goal="g",
        prompt="p",
        status="success",
        output=[
            NodeOutputEntry(
                seq=1,
                content="",
                tool_calls=[{"id": "call-1", "name": TOOL_ID, "arguments": {"path": "/a"}}],
                created_at=now_iso(),
            )
        ],
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    record = TaskProjectionToolResultRecord(
        task_id=TASK_ID,
        node_id=NODE_ID,
        tool_call_id="call-1",
        tool_name=TOOL_ID,
        status="success",
        payload={"parsed_payload": {}, "contract_delivery": "staged"},
    )

    trace = build_execution_trace(node, tool_results=[record], live_tool_calls=[])

    assert trace["tool_steps"][0]["contract_delivery"] == "staged"
