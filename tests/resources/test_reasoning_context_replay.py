"""思考内容进上下车的咽喉点：链级闸门、两条协议的落盘、投影、发送侧原位透传与白名单。

契约本体见 docs/architecture/runtime-overview.md「思考内容（reasoning）的上下文回放」。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from g3ku.config.schema import ManagedModelConfig
from g3ku.providers.responses_protocol_helpers import (
    _consume_sse,
    _convert_messages,
    _sanitize_tool_call_history,
)
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract
from main.models import NodeOutputEntry, NodeRecord, TaskRecord, TokenUsageSummary
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.runtime.chat_backend import (
    build_actual_request_diagnostics,
    build_prompt_cache_diagnostics,
    deepseek_api_base_family,
    deepseek_reasoning_pad_required,
    model_chain_replays_reasoning,
    sanitize_provider_messages,
)
from main.runtime.react_loop import ReActToolLoop
from main.storage.sqlite_store import SQLiteTaskStore

_REASONING_ITEM = {
    "type": "reasoning",
    "id": "rs_1",
    "summary": [],
    "encrypted_content": "ENC1",
    "g3ku_reasoning_model": "negi-r1",
}


class _StubConfig:
    """只提供 get_model_runtime_profile，判据不该依赖真实 Config。"""

    def __init__(self, profiles: dict[str, object | None]) -> None:
        self._profiles = profiles

    def get_model_runtime_profile(self, model_key: str | None = None):
        return self._profiles.get(str(model_key or "").strip())


def _profile(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(reasoning_context_enabled=enabled)


def test_replay_capability_defaults_on_per_binding() -> None:
    assert ManagedModelConfig(key="demo", llm_config_id="c1").reasoning_context_enabled is True
    assert (
        model_chain_replays_reasoning(
            _StubConfig({"a": ManagedModelConfig(key="a", llm_config_id="c1"), "b": ManagedModelConfig(key="b", llm_config_id="c1")}),
            ["a", "b"],
        )
        is True
    )
    assert (
        model_chain_replays_reasoning(
            _StubConfig({"a": ManagedModelConfig(key="a", llm_config_id="c1", reasoning_context_enabled=False)}),
            ["a"],
        )
        is False
    )
    # 解析不到 profile 的 ref 按关处理：闸门宁可少带，也不能把整条链打成畸形请求。
    assert model_chain_replays_reasoning(_StubConfig({}), ["a"]) is False


def test_stream_collects_both_reasoning_dialects() -> None:
    from g3ku.providers.streaming_timeouts import (
        StreamingDiagnostics,
        consume_openai_like_chat_stream,
    )

    async def run(key: str) -> str | None:
        async def gen():
            yield SimpleNamespace(choices=[{"delta": {key: "先算 2+3"}}])
            yield SimpleNamespace(choices=[{"delta": {"content": "等于 5"}}])

        _content, _calls, _finish, _usage, reasoning = await consume_openai_like_chat_stream(
            gen(),
            diagnostics=StreamingDiagnostics.start("openai_chat"),
            first_chunk_timeout_seconds=5,
            idle_chunk_timeout_seconds=5,
        )
        return reasoning

    assert asyncio.run(run("reasoning_content")) == "先算 2+3"
    assert asyncio.run(run("reasoning")) == "先算 2+3"


def test_chain_predicate_requires_every_ref_to_allow_replay() -> None:
    both_on = _StubConfig({"a": _profile(True), "b": _profile(True)})
    assert model_chain_replays_reasoning(both_on, ["a", "b"]) is True

    one_off = _StubConfig({"a": _profile(True), "b": _profile(False)})
    assert model_chain_replays_reasoning(one_off, ["a", "b"]) is False

    assert model_chain_replays_reasoning(_StubConfig({}), ["a"]) is False
    assert model_chain_replays_reasoning(both_on, []) is False


def test_sanitize_carries_both_shapes_only_on_assistant_rows() -> None:
    sanitized = sanitize_provider_messages(
        [
            {
                "role": "user",
                "content": "继续",
                "reasoning_content": "不该出现",
                "reasoning_items": [_REASONING_ITEM],
            },
            {
                "role": "assistant",
                "content": "先看索引",
                "reasoning_content": "我要再核一遍来源",
                "reasoning_items": [_REASONING_ITEM],
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
                ],
            },
            {"role": "assistant", "content": "空思考", "reasoning_content": "   ", "reasoning_items": []},
        ]
    )

    assert sanitized[0] == {"role": "user", "content": "继续"}
    assert sanitized[1]["reasoning_content"] == "我要再核一遍来源"
    assert sanitized[1]["reasoning_items"] == [_REASONING_ITEM]
    assert "reasoning_content" not in sanitized[2]
    assert "reasoning_items" not in sanitized[2]
    # 咽喉点必须幂等：诊断与线上体走同一个投影，第二次过不能把字段洗掉。
    assert sanitize_provider_messages(sanitized) == sanitized


def test_durable_projections_preserve_thinking_in_place() -> None:
    rows = [
        {
            "role": "assistant",
            "content": "先看索引",
            "reasoning_content": "我要再核一遍来源",
            "reasoning_items": [_REASONING_ITEM],
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
            ],
        },
        {"role": "assistant", "content": "正文", "reasoning_content": "只想了一段"},
        {"role": "user", "content": "继续"},
    ]

    seed = CeoMessageBuilder._request_body_seed_records(rows)
    assert [item.get("reasoning_content") for item in seed] == ["我要再核一遍来源", "只想了一段", None]
    assert seed[0]["reasoning_items"] == [_REASONING_ITEM]
    assert "reasoning_items" not in seed[1]

    prompt = CeoMessageBuilder._prompt_message_records(rows)
    assert prompt[0]["reasoning_content"] == "我要再核一遍来源"
    assert prompt[0]["reasoning_items"] == [_REASONING_ITEM]
    # 只有思考、没有正文也没有工具调用的行仍按现状丢弃（计划 §2.2 第 1 条）。
    assert (
        CeoMessageBuilder._prompt_message_records(
            [{"role": "assistant", "content": "", "reasoning_content": "半截思考"}]
        )
        == []
    )


def test_sanitize_provider_messages_reattaches_both_reasoning_shapes() -> None:
    sanitized = sanitize_provider_messages(
        [
            {"role": "user", "content": "继续"},
            {
                "role": "assistant",
                "content": "先看索引",
                "reasoning_content": "我要再核一遍来源",
                "reasoning_items": [_REASONING_ITEM],
            },
        ]
    )

    assert "reasoning_content" not in sanitized[0]
    assert sanitized[1]["reasoning_content"] == "我要再核一遍来源"
    assert sanitized[1]["reasoning_items"] == [_REASONING_ITEM]


def _runner() -> CeoFrontDoorRunner:
    return CeoFrontDoorRunner(loop=SimpleNamespace())


def test_gate_verdict_travels_from_response_to_history_row() -> None:
    runner = _runner()
    message = {
        "content": "先看索引",
        "reasoning_content": "我要再核一遍来源",
        "reasoning_items": [_REASONING_ITEM],
        "reasoning_context_allowed": True,
    }

    payload = runner._checkpoint_safe_model_response_payload(message)
    assert payload["reasoning_context_allowed"] is True
    assert payload["reasoning_items"] == [_REASONING_ITEM]

    field = runner._frontdoor_assistant_reasoning_field(payload)
    assert field["reasoning_content"] == "我要再核一遍来源"
    assert field["reasoning_items"] == [_REASONING_ITEM]

    blocked = dict(payload, reasoning_context_allowed=False)
    assert runner._frontdoor_assistant_reasoning_field(blocked) == {}
    blank = dict(payload, reasoning_content="   ", reasoning_items=[])
    assert runner._frontdoor_assistant_reasoning_field(blank) == {}


def test_node_row_helper_carries_thinking_whatever_happened_to_the_body() -> None:
    helper = ReActToolLoop._node_assistant_reasoning_field
    allowed = SimpleNamespace(
        reasoning_content="我要再核一遍来源",
        reasoning_items=[_REASONING_ITEM],
        reasoning_context_allowed=True,
    )
    blocked = SimpleNamespace(reasoning_content="我要再核一遍来源", reasoning_context_allowed=False)

    field = helper(None, allowed)
    assert field == {"reasoning_content": "我要再核一遍来源", "reasoning_items": [_REASONING_ITEM]}
    # 闸门关着 ⇒ 什么都不写；正文外置与否不再参与判定（操作者 2026-10-02 定的口径）。
    assert helper(None, blocked) == {}
    assert helper(None, SimpleNamespace(reasoning_content="", reasoning_context_allowed=True)) == {}


def _ledger_entry(**overrides) -> NodeOutputEntry:
    payload = {
        "seq": 1,
        "content": "先看索引",
        "content_ref": "",
        "tool_calls": [{"id": "c1", "name": "exec", "arguments": {}}],
        "reasoning_content": "我要再核一遍来源",
        "reasoning_items": [_REASONING_ITEM],
        "created_at": "2026-10-02T10:00:00",
    }
    payload.update(overrides)
    return NodeOutputEntry(**payload)


def test_resumed_row_reattaches_thinking_from_the_output_ledger() -> None:
    node = SimpleNamespace(output=[_ledger_entry()])
    pending = [{"id": "c1", "name": "exec"}]

    field = ReActToolLoop._pending_tool_turn_reasoning_field(None, node=node, pending_tool_calls=pending)
    assert field == {"reasoning_content": "我要再核一遍来源", "reasoning_items": [_REASONING_ITEM]}

    # 另一跳的条目（call_id 序列不同）不该被当成这一跳的思路。
    other = SimpleNamespace(output=[_ledger_entry(tool_calls=[{"id": "c9", "name": "exec"}])])
    assert (
        ReActToolLoop._pending_tool_turn_reasoning_field(None, node=other, pending_tool_calls=pending)
        == {}
    )
    # 没有思路的存量条目：恢复行照建，不带字段。
    plain = SimpleNamespace(output=[_ledger_entry(reasoning_content="", reasoning_items=[])])
    assert (
        ReActToolLoop._pending_tool_turn_reasoning_field(None, node=plain, pending_tool_calls=pending)
        == {}
    )


class _StreamResponse:
    def __init__(self, events: list[dict]) -> None:
        self._lines = []
        for event in events:
            self._lines.append(f"data: {json.dumps(event, ensure_ascii=False)}")
            self._lines.append("")

    async def aiter_lines(self):
        for line in self._lines:
            yield line


async def test_responses_stream_keeps_reasoning_items_with_model_stamp() -> None:
    events = [
        {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1", "summary": []}},
        {
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC1"},
        },
        {"type": "response.output_text.delta", "delta": "先看索引"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "model": "negi-r1",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "output": [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC1"}],
            },
        },
    ]

    content, tool_calls, finish_reason, usage, reasoning_items = await _consume_sse(_StreamResponse(events))

    assert content == "先看索引"
    assert finish_reason == "stop"
    assert len(reasoning_items) == 1
    assert reasoning_items[0]["encrypted_content"] == "ENC1"
    # 密文绑模型：戳是回放资格，也是唯一被我们塞进去的记账字段。
    assert reasoning_items[0]["g3ku_reasoning_model"] == "negi-r1"


def _assistant_row(**overrides) -> dict:
    row = {
        "role": "assistant",
        "content": "先看索引",
        "reasoning_items": [_REASONING_ITEM],
    }
    row.update(overrides)
    return row


def test_responses_input_replays_reasoning_only_to_the_issuing_model() -> None:
    _system, items = _convert_messages([{"role": "user", "content": "继续"}, _assistant_row()], model="negi-r1")

    # 思考项排在它所属的正文之前，这一条顺序就是契约。
    assert [item.get("role") for item in items] == ["user", None, "assistant"]
    replayed = items[1]
    assert replayed["type"] == "reasoning"
    assert replayed["encrypted_content"] == "ENC1"
    assert "g3ku_reasoning_model" not in replayed

    _system, swapped = _convert_messages([_assistant_row()], model="other-model")
    assert [item.get("type") for item in swapped] == ["message"]

    _system, unstamped = _convert_messages(
        [_assistant_row(reasoning_items=[{k: v for k, v in _REASONING_ITEM.items() if k != "g3ku_reasoning_model"}])],
        model="negi-r1",
    )
    assert [item.get("type") for item in unstamped] == ["message"]


def test_dangling_tool_call_row_survives_when_it_only_carries_reasoning() -> None:
    # 声明了工具调用却没有任何工具结果：调用被摘掉，行本身因带着思考而留下。
    kept = _sanitize_tool_call_history(
        [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_x", "function": {"name": "exec"}}],
          "reasoning_items": [_REASONING_ITEM]}]
    )

    assert len(kept) == 1
    assert "tool_calls" not in kept[0]
    assert kept[0]["reasoning_items"] == [_REASONING_ITEM]

    dropped = _sanitize_tool_call_history(
        [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_x", "function": {"name": "exec"}}]}]
    )
    assert dropped == []


def test_ledger_round_trip_stores_and_returns_the_hop_thinking(tmp_path) -> None:
    """账本这一环：写入 kwargs → NodeOutputEntry 字段 → 恢复行的取回，三段各真跑一次。"""
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    task_id, node_id = "task:reasoningledger", "node:a"
    stamp = "2026-10-02T10:00:00+08:00"
    store.upsert_task(
        TaskRecord(
            task_id=task_id, session_id="web:shared", title="demo", user_request="demo",
            status="in_progress", root_node_id=node_id, max_depth=1, created_at=stamp,
            updated_at=stamp, token_usage=TokenUsageSummary(tracked=True), metadata={},
        )
    )
    store.upsert_node(
        NodeRecord(
            node_id=node_id, task_id=task_id, parent_node_id=None, root_node_id=node_id, depth=0,
            node_kind="execution", status="in_progress", goal="demo", prompt="demo", input="demo",
            output=[], check_result="", final_output="", can_spawn_children=False, created_at=stamp,
            updated_at=stamp, token_usage=TokenUsageSummary(tracked=True),
        )
    )
    log_service = TaskLogService(
        store=store,
        file_store=TaskFileStore(tmp_path / "files"),
        registry=None,
        event_history_enabled=False,
    )

    field = ReActToolLoop._node_assistant_reasoning_field(
        None,
        SimpleNamespace(
            reasoning_content="我要再核一遍来源",
            reasoning_items=[_REASONING_ITEM],
            reasoning_context_allowed=True,
        ),
    )
    # 与 react_loop 账本写入点同形的两个 kwarg。
    log_service.append_node_output(
        task_id,
        node_id,
        content="先看索引",
        tool_calls=[{"id": "c1", "name": "exec", "arguments": {}}],
        reasoning_content=str(field.get("reasoning_content") or ""),
        reasoning_items=list(field.get("reasoning_items") or []),
    )

    node = store.get_node(node_id)
    entry = node.output[-1]
    assert entry.reasoning_content == "我要再核一遍来源"
    assert entry.reasoning_items == [_REASONING_ITEM]
    assert (
        ReActToolLoop._pending_tool_turn_reasoning_field(
            None, node=node, pending_tool_calls=[{"id": "c1", "name": "exec"}]
        )
        == field
    )


def test_node_ledger_call_site_wires_the_thinking_fields() -> None:
    """钉住调用点的接线：账本写入必须把当跳思考的两个字段递下去。

    这一环只能按源码断言——`ReActToolLoop.run()` 里那一次调用要跑完整回合才能触达，
    而漏掉这两个 kwarg 的表现是"恢复的行没有思路"，行为测试覆盖不到它。
    """
    source = Path(__file__).resolve().parents[2].joinpath("main/runtime/react_loop.py").read_text(encoding="utf-8")
    writer = source.split("self._log_service.append_node_output(", 1)[1][:1500]

    assert "reasoning_content=str(ledger_reasoning_field.get('reasoning_content') or '')," in writer
    assert "reasoning_items=list(ledger_reasoning_field.get('reasoning_items') or [])," in writer


# --- 发送侧目标限定补空（DeepSeek 思考模式 400 兼容，pi-ai 同形） -------------------------
# 合同与实测边界见 work/deepseek-reasoning-compat-plan.md §1：DeepSeek 只查字段存在、不查
# 内容，缺字段的 assistant 补 "" 即过；触发校验的不一定是末条，须补全部缺失行。


class _PadStubConfig:
    """谓词夹具：profile 闸门 + api_base 目标族，不依赖真实 Config。"""

    def __init__(self, profiles, api_bases=None):
        self._profiles = profiles
        self._api_bases = dict(api_bases or {})

    def get_model_runtime_profile(self, model_key=None):
        return self._profiles.get(str(model_key or "").strip())

    def get_api_base(self, model_key=None):
        return self._api_bases.get(str(model_key or "").strip())


_DEEPSEEK_BASE = "https://api.deepseek.com/v1"


def _poisoned_history():
    """仿 DSH #11029 毒历史：缺字段 assistant 在历史中段（非末条），尾随 tool 结果。"""
    return [
        {"role": "system", "content": "你是 Negi"},
        {"role": "user", "content": "查一下天气"},
        {
            "role": "assistant", "content": "", "reasoning_content": "先取日期",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_date", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "2026-11-14"},
        # 倒数第二条 assistant：带 tool_calls、缺 reasoning_content —— 实测触发校验的那条。
        {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "晴 7~13°C"},
        {"role": "assistant", "content": "今天晴，7~13°C。"},
    ]


def test_pad_fills_every_missing_assistant_field_and_keeps_real_text() -> None:
    rows = _poisoned_history()
    padded = sanitize_provider_messages(rows, pad_reasoning=True)
    assistants = [m for m in padded if m["role"] == "assistant"]
    assert all("reasoning_content" in m for m in assistants)
    assert assistants[0]["reasoning_content"] == "先取日期"  # 真文本原样，绝不截断
    assert assistants[1]["reasoning_content"] == ""  # 中段缺字段补空
    assert assistants[2]["reasoning_content"] == ""  # 轮末行也补
    assert all("reasoning_content" not in m for m in padded if m["role"] != "assistant")
    # 幂等：同一 pad 值再过一遍不变（诊断与线上体走同一投影）。
    assert sanitize_provider_messages(padded, pad_reasoning=True) == padded
    # pad=False 与今天逐字节一致：缺的仍缺。
    plain = sanitize_provider_messages(rows)
    assert "reasoning_content" not in plain[4]
    assert plain == sanitize_provider_messages(plain)


def test_pad_preserves_reasoning_items_and_whitespace_becomes_empty() -> None:
    rows = [
        {"role": "assistant", "content": "x", "reasoning_content": "   ", "reasoning_items": [_REASONING_ITEM]},
        {"role": "assistant", "content": "y"},
    ]
    padded = sanitize_provider_messages(rows, pad_reasoning=True)
    assert padded[0]["reasoning_content"] == ""
    assert padded[0]["reasoning_items"] == [_REASONING_ITEM]
    assert padded[1]["reasoning_content"] == ""
    assert "reasoning_items" not in padded[1]


def test_pad_predicate_is_target_scoped_and_fail_closed() -> None:
    cfg = _PadStubConfig(
        {"ds": _profile(True), "off": _profile(False), "other": _profile(True)},
        {"ds": _DEEPSEEK_BASE, "off": _DEEPSEEK_BASE, "other": "https://api.example.com/v1"},
    )
    assert deepseek_reasoning_pad_required(cfg, "ds") is True
    assert deepseek_reasoning_pad_required(cfg, "off") is False  # flag-off 绝不补
    assert deepseek_reasoning_pad_required(cfg, "other") is False  # 非 DeepSeek 域
    assert deepseek_reasoning_pad_required(cfg, "missing") is False  # profile 解析不到＝fail-closed
    assert deepseek_reasoning_pad_required(cfg, "") is False
    # target.api_base 优先（发送侧拿 target，合同侧拿 config，两者解析同一绑定）。
    target = SimpleNamespace(api_base=_DEEPSEEK_BASE, model_id="deepseek-chat")
    assert deepseek_reasoning_pad_required(_PadStubConfig({"ds": _profile(True)}, {}), "ds", target) is True
    # 网关 served 的 DeepSeek 靠 wire 模型名盖住（本机 catalog 全走网关，host 不含 deepseek.com）。
    gw_cfg = _PadStubConfig({"ds": _profile(True)}, {"ds": "https://gateway.internal/v1"})
    ds_via_gw = SimpleNamespace(api_base="https://gateway.internal/v1", model_id="deepseek-v4-flash")
    assert deepseek_reasoning_pad_required(gw_cfg, "ds", ds_via_gw) is True
    # 名字误导但实跑 qwen 的绑定不补：wire 模型名才是判据，不是 binding key。
    qwen_via_gw = SimpleNamespace(api_base="https://maas.aliyuncs.com/v1", model_id="qwen3.8-flash")
    assert deepseek_reasoning_pad_required(gw_cfg, "ds", qwen_via_gw) is False
    # 合同侧 target=None 经 resolve_chat_target 取模型名；stub 无 workspace_path ⇒ 解析失败 fail-closed。
    assert deepseek_reasoning_pad_required(gw_cfg, "ds") is False
    assert deepseek_api_base_family(_DEEPSEEK_BASE) is True
    assert deepseek_api_base_family("https://api.example.com") is False
    assert deepseek_api_base_family("") is False


def test_pad_model_name_branch_is_same_source_between_send_and_contract(monkeypatch) -> None:
    import main.runtime.chat_backend as cb

    cfg = _PadStubConfig({"ds": _profile(True)}, {"ds": "https://gateway.internal/v1"})
    monkeypatch.setattr(
        cb, "resolve_chat_target", lambda config, ref: SimpleNamespace(resolved_model="deepseek-v4-flash")
    )
    send_side = SimpleNamespace(api_base="https://gateway.internal/v1", model_id="deepseek-v4-flash")
    # 发送侧（target.model_id）与合同侧（resolver.resolved_model）必须同判。
    assert cb.deepseek_reasoning_pad_required(cfg, "ds", send_side) is True
    assert cb.deepseek_reasoning_pad_required(cfg, "ds") is True


def test_pad_keeps_wire_hash_and_diagnostics_same_source() -> None:
    rows = _poisoned_history()
    padded = sanitize_provider_messages(rows, pad_reasoning=True)
    direct = build_actual_request_diagnostics(request_messages=rows, pad_reasoning=True)
    via_cache = build_prompt_cache_diagnostics(
        stable_messages=rows, tool_schemas=None, provider_model="deepseek-chat",
        scope="ceo_frontdoor", pad_reasoning=True,
    )
    assert direct["actual_request_hash"] == via_cache["actual_request_hash"]
    # 哈希侧与线上体同源：诊断哈希 == 补空投影再诊断的哈希。
    assert direct["actual_request_hash"] == build_actual_request_diagnostics(
        request_messages=padded, pad_reasoning=True
    )["actual_request_hash"]
    # 补空改变字节：pad=True 与 pad=False 哈希不同（一次性缓存断点的来源）。
    plain = build_actual_request_diagnostics(request_messages=rows, pad_reasoning=False)
    assert plain["actual_request_hash"] != direct["actual_request_hash"]


def test_frontdoor_contract_pads_projection_identically() -> None:
    rows = _poisoned_history()
    contract = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor", provider_model="deepseek-chat",
        stable_messages=rows, dynamic_appendix_messages=[], tool_schemas=[],
        cache_family_revision=None, pad_reasoning=True,
    )
    assistants = [m for m in contract.request_messages if m.get("role") == "assistant"]
    assert assistants and all("reasoning_content" in m for m in assistants)
    wire = build_actual_request_diagnostics(request_messages=rows, pad_reasoning=True)
    assert contract.diagnostics["actual_request_hash"] == wire["actual_request_hash"]
    plain = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor", provider_model="deepseek-chat",
        stable_messages=rows, dynamic_appendix_messages=[], tool_schemas=[],
        cache_family_revision=None,
    )
    plain_assistants = [m for m in plain.request_messages if m.get("role") == "assistant"]
    assert "reasoning_content" not in plain_assistants[1]
