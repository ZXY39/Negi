"""阶段 0 观测面：wire `tools[]` 读数与按档计数的回归钉。

钉住四条合同：
1. 内容哈希与 `actual_tool_schema_hash` 同算法（对 tools[] 顺序盲），所以顺序必须由独立的
   序列哈希守——这是单源化验收里最容易被"看起来一致"骗过的一类变化；
2. 只在"名单与顺序都没变、字节却变了"时告警，并在消息里代入变化的名字；序列不同（跨过
   重印边界）或上一跳没有读数时不告警；
3. 前门渲染器逐名记来源，并把"需要兜底的名字数"作分母一起记——没有分母，命中 0 分不清
   "这档没用"与"这档没机会"；
4. 节点侧读数挂进 `model_visible_tool_selection_trace`（整份持久化，零归一化写点）。

两条车道用同一算法，读数才能对表；两侧内容哈希相同这一断言就是这条对齐的正证。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from loguru import logger

from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.utils.helpers import tools_bundle_single_source_enabled
from main.runtime import react_loop as rl


def _schema(name: str, description: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


class _LogCapture:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __enter__(self) -> "_LogCapture":
        self.handler_id = logger.add(
            lambda message: self.messages.append(str(message)),
            level="WARNING",
            format="{message}",
        )
        return self

    def __exit__(self, *exc_info: object) -> None:
        logger.remove(self.handler_id)


def test_wire_readings_content_hash_is_order_blind_but_sequence_hash_is_not() -> None:
    first = [_schema("alpha", "A"), _schema("beta", "B")]
    reordered = [_schema("beta", "B"), _schema("alpha", "A")]

    frontdoor = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(tools={}))
    readings_a = frontdoor._frontdoor_wire_tool_schema_readings(first)
    readings_b = frontdoor._frontdoor_wire_tool_schema_readings(reordered)
    assert readings_a["content_hash"] == readings_b["content_hash"]
    assert readings_a["sequence_hash"] != readings_b["sequence_hash"]
    assert set(readings_a["item_hashes"]) == {"alpha", "beta"}

    node_a = rl.ReActToolLoop._node_wire_tool_schema_readings(first)
    node_b = rl.ReActToolLoop._node_wire_tool_schema_readings(reordered)
    assert node_a["content_hash"] == readings_a["content_hash"]
    assert node_a["sequence_hash"] != node_b["sequence_hash"]


def test_frontdoor_warns_only_when_membership_same_and_bytes_differ() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(tools={}))
    runner_cls = type(runner)
    original = runner_cls._frontdoor_latest_actual_request_record
    schemas = [_schema("alpha", "A"), _schema("beta", "B")]
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:test"))
    state: dict = {}
    previous: dict = {}
    runner_cls._frontdoor_latest_actual_request_record = classmethod(lambda cls, **kwargs: dict(previous))
    try:
        with _LogCapture() as capture:
            runner._record_frontdoor_wire_tool_schema_readings(
                session=session, state=state, tool_schemas=schemas
            )
            # 首跳没有可比对象：不告警，但读数必须落进 state（没比对不等于通过）
            assert capture.messages == []
            assert state["frontdoor_wire_tool_name_sequence_hash"]

            previous = {
                "wire_tool_name_sequence_hash": state["frontdoor_wire_tool_name_sequence_hash"],
                "wire_tool_schema_hash": "0" * 64,
                "wire_tool_schema_item_hashes": {
                    "alpha": "deadbeef00000000",
                    "beta": state["frontdoor_wire_tool_schema_item_hashes"]["beta"],
                },
            }
            runner._record_frontdoor_wire_tool_schema_readings(
                session=session, state=state, tool_schemas=schemas
            )
            assert len(capture.messages) == 1
            assert "outside a reprint boundary" in capture.messages[0]
            assert "changed=alpha" in capture.messages[0]
            assert "web:test" in capture.messages[0]

            previous = {
                "wire_tool_name_sequence_hash": state["frontdoor_wire_tool_name_sequence_hash"],
                "wire_tool_schema_hash": state["frontdoor_wire_tool_schema_hash"],
                "wire_tool_schema_item_hashes": dict(state["frontdoor_wire_tool_schema_item_hashes"]),
            }
            runner._record_frontdoor_wire_tool_schema_readings(
                session=session, state=state, tool_schemas=schemas
            )
            assert len(capture.messages) == 1

            # 名单换了＝重印边界，本来就该变字节，不算回归
            previous = {
                "wire_tool_name_sequence_hash": "f" * 64,
                "wire_tool_schema_hash": "0" * 64,
                "wire_tool_schema_item_hashes": {},
            }
            runner._record_frontdoor_wire_tool_schema_readings(
                session=session,
                state=state,
                tool_schemas=[_schema("gamma", "G")],
            )
            assert len(capture.messages) == 1
    finally:
        runner_cls._frontdoor_latest_actual_request_record = original


@pytest.mark.skipif(tools_bundle_single_source_enabled(), reason="单源化开关打开后这一档退役：它钉的是回填链的取数次序")
def test_frontdoor_renderer_counts_schema_sources_with_denominator() -> None:
    class _LiveTool:
        name = "live_tool"
        description = ""
        parameters: dict = {}

        def to_model_schema(self) -> dict:
            return _schema("live_tool", "live")

    class _DeclaredOnlyTool:
        name = "declared_tool"
        description = ""
        parameters: dict = {}

        def to_model_schema(self) -> dict:
            return _schema("declared_tool", "declared")

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:renderer-counts"),
        _frontdoor_pinned_tool_schema_bundle={"bundled_tool": _schema("bundled_tool", "bundled")},
    )
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(tools={"live_tool": _LiveTool()}))
    runner._frontdoor_execution_bundle = lambda **kwargs: SimpleNamespace(
        declared_tools={"declared_tool": _DeclaredOnlyTool()}
    )
    state = {
        "provider_tool_names": ["live_tool", "declared_tool", "bundled_tool", "vanished_tool"],
        "session_key": "web:renderer-counts",
        "rbac_visible_tool_names": [],
        "messages": [],
        "frontdoor_stage_state": {},
    }

    rendered = runner._frontdoor_tool_schemas_for_state(
        state=state, runtime=SimpleNamespace(context=SimpleNamespace(session=session))
    )

    # 名单 4 名 + 渲染器尾部追加的合成控制工具；控制工具本例既不在 declared 也不在注册表，
    # 因此落占位档——这正是 N5 说的"前门集合口径与节点不同"。
    assert state["frontdoor_tool_schema_hits"] == {
        "declared": 1,
        "registry": 1,
        "retired": 0,
        "bundle": 1,
        "placeholder": 2,
    }
    assert state["frontdoor_tool_schema_fallback_opportunities"] == 3
    assert [(item.get("function") or {}).get("name") for item in rendered][-1:] == ["silent"]


def test_node_readings_land_in_trace_and_warn_on_byte_drift_without_membership_change() -> None:
    readings = rl.ReActToolLoop._node_wire_tool_schema_readings([_schema("alpha", "A")])
    frame_trace = {
        "wire_tool_name_sequence_hash": readings["sequence_hash"],
        "wire_tool_schema_hash": "0" * 64,
        "wire_tool_schema_item_hashes": {"alpha": "ffffffffffffffff"},
    }
    loop = SimpleNamespace()
    loop._node_wire_tool_schema_readings = rl.ReActToolLoop._node_wire_tool_schema_readings
    loop._runtime_frame = lambda task_id, node_id: {
        "model_visible_tool_selection_trace": dict(frame_trace)
    }
    selection: dict = {"trace": {}}

    with _LogCapture() as capture:
        rl.ReActToolLoop._record_node_wire_tool_schema_readings(
            loop,
            task_id="task-node",
            node_id="node-node",
            tool_schema_selection=selection,
            tool_schemas=[_schema("alpha", "A")],
        )
        assert len(capture.messages) == 1
        assert "outside a reprint boundary" in capture.messages[0]
        assert "changed=alpha" in capture.messages[0]

    # 读数以 trace 字段的形式交付：`model_visible_tool_selection_trace` 整份持久化，
    # 新增帧顶层字段要跟七个归一化写点，漏一个就是"存了等于没存"。
    trace = selection["trace"]
    assert trace["wire_tool_schema_hash"] == readings["content_hash"]
    assert trace["wire_tool_name_sequence_hash"] == readings["sequence_hash"]

    frame_trace["wire_tool_schema_hash"] = trace["wire_tool_schema_hash"]
    frame_trace["wire_tool_schema_item_hashes"] = dict(trace["wire_tool_schema_item_hashes"])
    with _LogCapture() as capture:
        rl.ReActToolLoop._record_node_wire_tool_schema_readings(
            loop,
            task_id="task-node",
            node_id="node-node",
            tool_schema_selection=selection,
            tool_schemas=[_schema("alpha", "A")],
        )
        assert capture.messages == []
