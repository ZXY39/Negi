"""新增可读 skill 的持久提示：两车道的合同测试。

验收口径（`docs/FIX_PLAN_skill-roster-durable-notice.md`）：

- 提示落进上下文、**只注入一次**；检测与投递只发生在真正落请求体的那一处装配点。
- 尾块不再带 `new_readable_skills`（每跳重印那条道整段作废）。
- 头部名单与 `tools[]` 不因一次新增而改字节。
- 压缩跳把仍可读的名字带过压缩产物，同一次压缩只搬一份；不可读的在那一跳退场。
- 截断/Fork 撤回台账晚于截断点的记录，使该名字可以重新投递。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from g3ku.runtime import skill_roster_notice as notice
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor.tool_contract import (
    clear_pinned_contract_stores,
    frontdoor_pinned_contract_text,
    skill_roster_epoch_for,
)
from main.runtime.react_loop import ReActToolLoop

MARKER = notice.SKILL_ROSTER_NOTICE_MARKER
SESSION_KEY = "web:roster-notice"
SCOPE = f"session:{SESSION_KEY}"


@pytest.fixture(autouse=True)
def _isolate_stores():
    clear_pinned_contract_stores()
    notice.clear_skill_roster_notice_stores()
    yield
    clear_pinned_contract_stores()
    notice.clear_skill_roster_notice_stores()


def _item(skill_id: str) -> dict[str, str]:
    return {
        "skill_id": skill_id,
        "display_name": skill_id.replace("-", " ").title(),
        "description": f"{skill_id} 的描述。",
    }


def _frontdoor_runner(live_skills: list[dict[str, str]]):
    service = SimpleNamespace(
        list_contract_visible_skill_resources=lambda *, actor_role, session_id: list(live_skills),
        list_effective_tool_names=lambda *, actor_role, session_id: ["exec", "submit_next_stage"],
        _current_exec_runtime_policy_payload=lambda: {},
    )
    return create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=service))


def _notice_texts(messages: list[dict]) -> list[str]:
    return [
        str(item.get("content") or "")
        for item in list(messages or [])
        if isinstance(item, dict) and str(item.get("content") or "").startswith(MARKER)
    ]


def _pin_head(skill_ids: list[str]) -> str:
    """按真实前门口径钉头部：**裸 session_key**（`session:` 前缀只是台账作用域写法）。

    曾经把前缀键传给钉住表，测试里 covered 取得到、线上取不到，于是新会话第一跳
    把整份名单重播了一遍——键径必须与 `_frontdoor_pinned_contract` 一致。
    """
    return frontdoor_pinned_contract_text(
        None,
        skill_items=[_item(skill_id) for skill_id in skill_ids],
        exec_runtime_policy=None,
        session_temp_dir=None,
        skill_roster_epoch=skill_roster_epoch_for(None, session_key=SESSION_KEY),
        session_key=SESSION_KEY,
        include_extras=False,
    )


def test_frontdoor_first_hop_of_new_session_stays_silent() -> None:
    """新会话第一跳：头部已带全量名单 ⇒ 一条提示都不发（不重播名单）。"""
    head = _pin_head(["alpha", "beta"])
    runner = _frontdoor_runner([_item("alpha"), _item("beta")])
    messages = runner._apply_frontdoor_skill_roster_notice(
        [{"role": "system", "content": head}, {"role": "user", "content": "hi"}],
        session_key=SESSION_KEY,
    )
    assert _notice_texts(messages) == []
    assert notice.notice_context_for(SCOPE)["records"] == []


def test_frontdoor_notice_injected_once_and_appended_to_history_end() -> None:
    """头部已带的不宣布；新变可读的投恰好一条；投过之后同跳与下跳都不再投。"""
    history = [{"role": "system", "content": _pin_head(["alpha"])}, {"role": "user", "content": "hello"}]

    runner = _frontdoor_runner([_item("alpha")])
    assert _notice_texts(runner._apply_frontdoor_skill_roster_notice(history, session_key=SESSION_KEY)) == []

    runner = _frontdoor_runner([_item("alpha"), _item("beta")])
    injected = runner._apply_frontdoor_skill_roster_notice(history, session_key=SESSION_KEY)
    texts = _notice_texts(injected)
    assert len(texts) == 1
    assert "- `beta` | Beta | beta 的描述。" in texts[0]
    assert "alpha" not in texts[0]
    # 位置：本轮历史末尾（运行时块由别处追加在它之后）
    assert injected[-1]["role"] == "user"

    again = _frontdoor_runner([_item("alpha"), _item("beta")])._apply_frontdoor_skill_roster_notice(
        injected, session_key=SESSION_KEY
    )
    assert len(_notice_texts(again)) == 1  # 不叠第二份
    assert len(again) == len(injected)


def test_frontdoor_notice_does_not_touch_pinned_head() -> None:
    """一次新增不许改头部字节：钉住块仍只按 roster epoch 动。"""
    first = frontdoor_pinned_contract_text(
        None,
        skill_items=[_item("alpha")],
        exec_runtime_policy=None,
        session_temp_dir=None,
        skill_roster_epoch=skill_roster_epoch_for(None, session_key=SESSION_KEY),
        session_key=SESSION_KEY,
        include_extras=False,
    )
    runner = _frontdoor_runner([_item("alpha"), _item("beta")])
    runner._apply_frontdoor_skill_roster_notice(
        [{"role": "user", "content": "hello"}], session_key=SESSION_KEY
    )
    second = frontdoor_pinned_contract_text(
        None,
        skill_items=[_item("alpha"), _item("beta")],
        exec_runtime_policy=None,
        session_temp_dir=None,
        skill_roster_epoch=skill_roster_epoch_for(None, session_key=SESSION_KEY),
        session_key=SESSION_KEY,
        include_extras=False,
    )
    assert second == first
    assert "`beta`" not in second
    assert MARKER not in second


def test_detection_and_delivery_has_a_single_reader() -> None:
    """结构闸门：台账只在发送准备这一处被读。

    旧机制的失效原因就是一条回合内四个装配点共用一次消耗式读——先跑的把差集领走，
    落请求体那处永远拿空。这条测试把"读者唯一"钉住，防其它装配点再引入投递。
    """
    from g3ku.runtime.frontdoor import _ceo_runtime_ops as runtime_ops

    send_source = inspect.getsource(runtime_ops.CeoFrontDoorRuntimeOps._frontdoor_send_preflight_snapshot)
    assert "_apply_frontdoor_skill_roster_notice" in send_source

    for target in (
        create_agent_impl.CreateAgentCeoFrontDoorRunner._frontdoor_prompt_contract,
        create_agent_impl.CreateAgentCeoFrontDoorRunner._dynamic_appendix_message_records_for_state,
        runtime_ops.CeoFrontDoorRuntimeOps._graph_prepare_turn,
    ):
        source = inspect.getsource(target)
        assert "deliver_for_scope" not in source
        assert "_apply_frontdoor_skill_roster_notice" not in source
        assert "new_readable" not in source


def test_tail_contract_blocks_no_longer_render_the_roster_line() -> None:
    """两车道尾块都不再渲 `new_readable_skills`。"""
    from g3ku.runtime.frontdoor.tool_contract import _render_frontdoor_contract_summary

    rendered = _render_frontdoor_contract_summary(
        {
            "candidate_tools": [{"tool_id": "web_fetch"}],
            "declared_tool_names": [],
            "new_readable_skill_items": [{"skill_id": "beta", "display_name": "Beta", "description": "d"}],
        }
    )
    assert "new_readable_skills" not in rendered

    loop = _node_loop()
    contract = loop._build_node_dynamic_contract(
        node=_node(),
        message_history=[{"role": "system", "content": "node system"}],
        tool_schema_selection={
            "tool_names": ["submit_next_stage"],
            "candidate_tool_names": [],
            "lightweight_tool_ids": [],
            "trace": {},
        },
        runtime_context={"live_skill_roster_supplier": lambda: [_item("beta")]},
        skill_roster_items=[_item("beta")],
    )
    assert "new_readable_skills" not in contract.to_message()["content"]


def test_notice_message_survives_durable_baseline() -> None:
    """提示必须活过 durable 基线（"只投一次"的地基），而运行时块照样被剥掉。"""
    runner = _frontdoor_runner([_item("beta")])
    context, _ = notice.record_live_skills({}, [_item("beta")], now="2026-10-19T10:00:00+08:00")
    message = notice.build_notice_message(notice.pending_notice_records(context))
    assert message is not None
    body = [
        {"role": "system", "content": "ceo system"},
        {"role": "user", "content": "hello"},
        message,
        {"role": "user", "content": "## Runtime Tool Contract\nkind: frontdoor_runtime_tool_contract"},
        {"role": "user", "content": "## Runtime Stage Gate\nkind: frontdoor_runtime_stage_gate"},
    ]
    durable = runner._durable_frontdoor_request_body_messages(body)
    texts = [str(item.get("content") or "") for item in durable]
    assert any(text.startswith(MARKER) for text in texts)
    assert not any(text.startswith("## Runtime Tool Contract") for text in texts)


def test_notice_echo_is_stripped_without_eating_the_answer() -> None:
    echoed = f"这是回答。\n\n{MARKER}\n{notice.SKILL_ROSTER_NOTICE_HEADING}\n- `beta` | Beta | beta 的描述。"
    assert notice.strip_skill_roster_notice_echo(echoed) == "这是回答。"
    assert notice.strip_skill_roster_notice_echo("普通回答，不含提示") == "普通回答，不含提示"
    assert notice.is_skill_roster_notice_text(f"{MARKER}\n{notice.SKILL_ROSTER_NOTICE_HEADING}") is True
    assert notice.is_skill_roster_notice_text("## Runtime Tool Contract") is False


def test_compression_carry_is_once_per_stage_and_retires_unreadable() -> None:
    context = notice.normalize_skill_roster_notice_context({})
    context, _fresh = notice.record_live_skills(
        context, [_item("beta"), _item("gamma")], now="2026-10-19T10:00:00+08:00"
    )
    pending = notice.pending_notice_records(context)
    context = notice.mark_records_delivered(
        context, [item["skill_id"] for item in pending], now="2026-10-19T10:00:05+08:00"
    )

    carried_context, carried = notice.carry_records_for_compression(
        context,
        compression_stage_id="turn-1#comp",
        readable_skill_ids=["beta"],
        now="2026-10-19T10:01:00+08:00",
    )
    assert [item["skill_id"] for item in carried] == ["beta"]
    message = notice.build_carried_message(carried)
    assert message is not None and notice.SKILL_ROSTER_CARRIED_HEADING in message["content"]
    assert "gamma" not in message["content"]
    retired = [item for item in carried_context["records"] if item["skill_id"] == "gamma"]
    assert retired[0]["superseded_at"] == "2026-10-19T10:01:00+08:00"

    again_context, again = notice.carry_records_for_compression(
        carried_context,
        compression_stage_id="turn-1#comp",
        readable_skill_ids=["beta"],
        now="2026-10-19T10:02:00+08:00",
    )
    assert again == []
    assert again_context["carried_stages"] == ["turn-1#comp"]


def test_truncation_drops_records_after_cutoff() -> None:
    context = {
        "records": [
            {"skill_id": "old", "recorded_at": "2026-10-19T09:00:00+08:00", "delivered_at": "2026-10-19T09:00:01+08:00"},
            {"skill_id": "new", "recorded_at": "2026-10-19T11:00:00+08:00", "delivered_at": "2026-10-19T11:00:01+08:00"},
        ],
        "carried_stages": ["turn-9#comp"],
    }
    rolled = notice.drop_records_after(context, cutoff_iso="2026-10-19T10:00:00+08:00")
    assert [item["skill_id"] for item in rolled["records"]] == ["old"]
    fully = notice.drop_records_after(context, cutoff_iso="")
    assert fully["records"] == [] and fully["carried_stages"] == []


class _FakeLogService:
    def __init__(self) -> None:
        self._frames: dict[tuple[str, str], dict] = {}

    def read_runtime_frame(self, task_id: str, node_id: str):
        return self._frames.get((task_id, node_id))

    def upsert_frame(self, task_id: str, frame: dict, publish_snapshot: bool = False) -> None:
        self._frames[(task_id, str(frame.get("node_id") or ""))] = dict(frame)


def _node_loop() -> ReActToolLoop:
    return ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_FakeLogService(), max_iterations=2)


def _node() -> SimpleNamespace:
    return SimpleNamespace(task_id="task-notice", node_id="node-notice", metadata={}, node_kind="execution")


def test_node_notice_injected_once_per_node() -> None:
    """节点道同一形状：每跳检测、只投一次、与会话道共用一个渲染函数（字节同形）。"""
    loop = _node_loop()
    node = _node()
    messages = [{"role": "system", "content": "node system"}, {"role": "user", "content": "go"}]
    # 先按第一跳的可见集钉住头部（alpha 因此在头部里，不算"新宣布"）
    loop._apply_node_skill_roster_head(
        list(messages), scope_key=f"node:{node.task_id}:{node.node_id}", skill_items=[_item("alpha")]
    )

    first = loop._apply_node_skill_roster_notice(
        list(messages), node=node, skill_roster_items=[_item("alpha")]
    )
    assert _notice_texts(first) == []

    second = loop._apply_node_skill_roster_notice(
        list(messages), node=node, skill_roster_items=[_item("alpha"), _item("beta")]
    )
    injected = _notice_texts(second)
    assert len(injected) == 1 and "- `beta` |" in injected[0]
    assert injected[0] == notice.render_notice_text(
        [{"skill_id": "beta", "display_name": "Beta", "description": "beta 的描述。"}]
    )

    third = loop._apply_node_skill_roster_notice(
        second, node=node, skill_roster_items=[_item("alpha"), _item("beta")]
    )
    assert len(_notice_texts(third)) == 1
    assert len(third) == len(second)


def test_node_ledger_is_persisted_to_node_metadata() -> None:
    """节点台账必须落到 `node.metadata`：那一写是"重启/续跑后不重复投"的唯一依据。"""
    written: dict[str, dict] = {}

    class _MetadataLogService:
        def read_runtime_frame(self, task_id, node_id):
            return None

        def upsert_frame(self, task_id, frame, publish_snapshot=False):
            return None

        def update_node_metadata(self, node_id, metadata_mutator):
            next_metadata = metadata_mutator({})
            written[node_id] = next_metadata
            return SimpleNamespace(node_id=node_id, metadata=next_metadata)

    loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=_MetadataLogService(), max_iterations=2)
    node = _node()
    loop._apply_node_skill_roster_head(
        [{"role": "system", "content": "node system"}],
        scope_key=f"node:{node.task_id}:{node.node_id}",
        skill_items=[_item("alpha")],
    )
    loop._apply_node_skill_roster_notice(
        [{"role": "system", "content": "node system"}],
        node=node,
        skill_roster_items=[_item("alpha"), _item("beta")],
    )

    stored = written[node.node_id]["skill_roster_notice_context"]
    delivered = [record for record in stored["records"] if record["skill_id"] == "beta"]
    assert delivered and delivered[0]["delivered_at"]
    assert delivered[0]["superseded_at"] == ""

    # 重启语义：进程表清空后从 metadata 恢复，同一个 beta 不再投第二份
    notice.clear_skill_roster_notice_stores()
    node.metadata = {notice.SKILL_ROSTER_NOTICE_CONTEXT_KEY: stored}
    again = loop._apply_node_skill_roster_notice(
        [{"role": "system", "content": "node system"}],
        node=node,
        skill_roster_items=[_item("alpha"), _item("beta")],
    )
    assert _notice_texts(again) == []


def test_node_notice_message_lands_in_durable_history() -> None:
    """投递的那条要写进 `message_history`：只挂发送投影的话，重启/续跑重建历史后它就不在了，
    而台账已记"投过" ⇒ 那个名字从此再也不会出现。"""
    loop = _node_loop()
    node = _node()
    history = [{"role": "system", "content": "node system"}, {"role": "user", "content": "go"}]
    loop._apply_node_skill_roster_head(
        list(history), scope_key=f"node:{node.task_id}:{node.node_id}", skill_items=[_item("alpha")]
    )

    body = loop._apply_node_skill_roster_notice(
        list(history),
        node=node,
        message_history=history,
        skill_roster_items=[_item("alpha"), _item("beta")],
    )
    assert len(_notice_texts(body)) == 1
    assert len(history) == 3 and len(_notice_texts(history)) == 1
    assert history[-1] is not body[-1] and history[-1]["content"] == body[-1]["content"]

    # 下一跳的历史已经带着这条：不再补第二条，正文里也只有一份。
    next_body = loop._apply_node_skill_roster_notice(
        list(history),
        node=node,
        message_history=history,
        skill_roster_items=[_item("alpha"), _item("beta")],
    )
    assert len(history) == 3
    assert len(_notice_texts(next_body)) == 1


def test_continuity_snapshot_keeps_the_notice_ledger() -> None:
    """连续性侧车是按白名单装配的：台账不在名单里就永远落不了盘（重启即重复宣布）。"""
    from g3ku.runtime.web_ceo_sessions import _normalized_completed_continuity_snapshot

    context, _ = notice.record_live_skills({}, [_item("beta")], now="2026-10-19T10:00:00+08:00")
    context = notice.mark_records_delivered(context, ["beta"], now="2026-10-19T10:00:01+08:00")
    snapshot = _normalized_completed_continuity_snapshot(
        {
            "frontdoor_request_body_messages": [{"role": "user", "content": "hi"}],
            "skill_roster_notice_context": context,
        }
    )
    assert "skill_roster_notice_context" in snapshot
    records = snapshot["skill_roster_notice_context"]["records"]
    assert [item["skill_id"] for item in records] == ["beta"]
    assert records[0]["delivered_at"] == "2026-10-19T10:00:01+08:00"


def test_repair_required_skill_is_not_announced_until_it_loads() -> None:
    """`available=False`（repair-required）不算"新增可读"：宣布了模型也 load 不出正文。

    这类名字不进台账，所以被修好之后真可读的那一跳还是会宣布一次。
    """
    head = _pin_head(["alpha"])
    history = [{"role": "system", "content": head}, {"role": "user", "content": "hi"}]

    runner = _frontdoor_runner([{**_item("broken"), "available": False}, _item("alpha")])
    quiet = runner._apply_frontdoor_skill_roster_notice(history, session_key=SESSION_KEY)
    assert _notice_texts(quiet) == []
    assert notice.notice_context_for(SCOPE)["records"] == []

    fixed = _frontdoor_runner([{**_item("broken"), "available": True}, _item("alpha")])
    texts = _notice_texts(fixed._apply_frontdoor_skill_roster_notice(quiet, session_key=SESSION_KEY))
    assert len(texts) == 1 and "- `broken`" in texts[0]


def test_restore_without_the_notice_key_keeps_the_ledger() -> None:
    """载荷没带这个键时不许清空台账：手动压缩重写出来的基线载荷本来就不带它。"""
    from g3ku.runtime.session_agent import RuntimeAgentSession

    context, _ = notice.record_live_skills({}, [_item("beta")], now="2026-10-19T10:00:00+08:00")
    context = notice.mark_records_delivered(context, ["beta"], now="2026-10-19T10:00:01+08:00")
    notice.set_notice_context(SCOPE, context)

    state = SimpleNamespace(session_key=SESSION_KEY)
    stub = SimpleNamespace(
        state=state,
        _normalized_name_list=staticmethod(RuntimeAgentSession._normalized_name_list),
        _restorable_frontdoor_request_body_messages=staticmethod(
            RuntimeAgentSession._restorable_frontdoor_request_body_messages
        ),
        _adopt_compression_state=lambda *args: dict(args[-1] or {}),
        _recompute_frontdoor_completed_continuity_bridge_pending=lambda **_kwargs: None,
    )
    payload = {"frontdoor_request_body_messages": [{"role": "user", "content": "hi"}]}
    assert RuntimeAgentSession._restore_frontdoor_state_from_payload(
        stub, payload, source="completed_continuity", allow_continuity_bridge=False
    ) is True
    assert [r["skill_id"] for r in notice.notice_context_for(SCOPE)["records"]] == ["beta"]

    # 载荷带了键时以载荷为准（截断/回滚要靠这条把台账拉回该轮边界的样子）
    payload_with = dict(payload)
    payload_with["skill_roster_notice_context"] = {"records": [], "carried_stages": []}
    RuntimeAgentSession._restore_frontdoor_state_from_payload(
        stub, payload_with, source="completed_continuity", allow_continuity_bridge=False
    )
    assert notice.notice_context_for(SCOPE)["records"] == []


def test_manual_compression_carry_appends_to_the_durable_baseline() -> None:
    """手动压缩那一跳：仍可读的名字要补回 durable 基线，读不到的不补。"""
    runner = _frontdoor_runner([_item("beta")])
    context, _ = notice.record_live_skills({}, [_item("beta"), _item("gone")], now="2026-10-19T10:00:00+08:00")
    context = notice.mark_records_delivered(context, ["beta", "gone"], now="2026-10-19T10:00:01+08:00")
    notice.set_notice_context(SCOPE, context)
    session = SimpleNamespace(
        _frontdoor_request_body_messages=[{"role": "user", "content": "hi"}],
        _active_turn_id="turn-manual-1",
    )

    runner._carry_manual_compression_notice_into_baseline(session=session, session_key=SESSION_KEY)

    baseline = list(session._frontdoor_request_body_messages)
    assert len(baseline) == 2 and str(baseline[-1]["content"]).startswith(MARKER)
    assert "beta" in baseline[-1]["content"] and "gone" not in baseline[-1]["content"]
    # 同一次压缩不再补第二份
    runner._carry_manual_compression_notice_into_baseline(session=session, session_key=SESSION_KEY)
    assert len(session._frontdoor_request_body_messages) == 2


def test_frontdoor_delivery_publishes_one_internal_row_event() -> None:
    """会话流那边发一条内部行事件；它只是显示面，模型侧那份提示不依赖它。"""
    published: list[tuple[str, dict]] = []

    class _Registry:
        def next_ceo_seq(self, session_id: str) -> int:
            return 7

        def publish_ceo(self, session_id: str, envelope: dict) -> None:
            published.append((session_id, envelope))

    service = SimpleNamespace(
        list_contract_visible_skill_resources=lambda *, actor_role, session_id: [_item("alpha"), _item("beta")],
        list_effective_tool_names=lambda *, actor_role, session_id: ["exec", "submit_next_stage"],
        _current_exec_runtime_policy_payload=lambda: {},
        registry=_Registry(),
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=service))
    _pin_head(["alpha"])
    runner._apply_frontdoor_skill_roster_notice(
        [{"role": "user", "content": "hello"}], session_key=SESSION_KEY
    )

    assert len(published) == 1
    session_id, envelope = published[0]
    assert session_id == SESSION_KEY
    assert envelope["type"] == "ceo.skill.roster_notice"
    assert [item["skill_id"] for item in envelope["data"]["items"]] == ["beta"]
    assert envelope["seq"] == 7

    # 已宣布过的不再发第二条
    runner._apply_frontdoor_skill_roster_notice(
        [{"role": "user", "content": "hello"}], session_key=SESSION_KEY
    )
    assert len(published) == 1


def test_notice_copy_is_english_and_scope_bounded() -> None:
    """模型可见文案：英文、限定"本会话"、只给取正文入口，不写运行时承诺。"""
    text = notice.render_notice_text(
        [{"skill_id": "beta", "display_name": "Beta", "description": "beta 的描述。"}]
    )
    assert text.startswith(notice.SKILL_ROSTER_NOTICE_MARKER)
    assert "Skills newly readable in this session:" in text
    assert "load_skill_context(skill_id=" in text
    assert "in this session" in text
    assert "[G3KU_" not in text
    for promise in ("已为你保留", "will be kept for you", "I will"):
        assert promise not in text
    carried = notice.render_carried_text(
        [{"skill_id": "beta", "display_name": "Beta", "description": "beta 的描述。"}]
    )
    assert "kept across compaction" in carried
    assert "load_skill_context" not in carried


def test_node_compression_carry_appends_after_compacted_baseline() -> None:
    loop = _node_loop()
    node = _node()
    scope = f"node:{node.task_id}:{node.node_id}"
    context, _ = notice.record_live_skills({}, [_item("beta")], now="2026-10-19T10:00:00+08:00")
    context = notice.mark_records_delivered(context, ["beta"], now="2026-10-19T10:00:01+08:00")
    notice.set_notice_context(scope, context)

    baseline = [{"role": "system", "content": "node system"}, {"role": "user", "content": "[TOKEN_COMPACT_V2] 摘要"}]
    carried = loop._carry_node_skill_roster_notice_on_compaction(
        baseline, node=node, skill_roster_items=[_item("beta")]
    )
    assert len(carried) == len(baseline) + 1
    assert notice.SKILL_ROSTER_CARRIED_HEADING in carried[-1]["content"]
    again = loop._carry_node_skill_roster_notice_on_compaction(
        baseline, node=node, skill_roster_items=[_item("beta")]
    )
    assert len(again) == len(baseline)
