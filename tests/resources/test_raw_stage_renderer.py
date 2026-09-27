"""纯函数单测：g3ku/runtime/frontdoor/raw_stage_renderer.py。

被测公开函数：
- retained_raw_stage_messages(stage_state) -> (list[dict], set[str])
  保留集判据来自共享层 `stage_prompt_compaction.retained_completed_stage_ids`
  （只按标记，不再有条数窗口），本模块只负责渲染 raw 块。

覆盖：哪些阶段进 raw（未点名裁撤、未收口的终态普通阶段 + 活动阶段）、边界（空列表/空串/
非法类型容错）、调用方依赖的不变量（渲染稳定性、阶段顺序保持、与 STAGE_RAW_PREFIX 常量
一致、role 合同 system、JSON 键稳定排序、ensure_ascii=False）。
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from g3ku.runtime.frontdoor.raw_stage_renderer import retained_raw_stage_messages
from g3ku.runtime.stage_prompt_compaction import STAGE_RAW_PREFIX


def _stage(
    stage_id: str | None,
    stage_index: int,
    *,
    status: str = "completed",
    stage_kind: str = "normal",
    **overrides: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "stage_id": stage_id,
        "stage_index": stage_index,
        "status": status,
        "stage_kind": stage_kind,
        "key_refs": [],
        "rounds": [],
    }
    base.update(overrides)
    return base


def _stage_state(stages: list[Any], active_stage_id: str = "") -> dict[str, Any]:
    return {
        "active_stage_id": active_stage_id,
        "transition_required": False,
        "stages": list(stages),
    }


def _payload(message: dict[str, Any]) -> dict[str, Any]:
    content = message["content"]
    assert content.startswith(STAGE_RAW_PREFIX + "\n")
    return json.loads(content.split("\n", 1)[1])


def test_retained_raw_stage_messages_empty_state() -> None:
    messages, retained_ids = retained_raw_stage_messages(_stage_state([]))
    assert messages == []
    assert retained_ids == set()


def test_retained_raw_stage_messages_keeps_every_unmarked_completed_stage_regardless_of_count() -> None:
    # 窗口已移除：条数再多也不自动过期，只有标记能把阶段请出 raw。
    stages = [_stage(f"s{i + 1}", i + 1) for i in range(9)]
    messages, retained_ids = retained_raw_stage_messages(_stage_state(stages))
    assert retained_ids == {f"s{i + 1}" for i in range(9)}
    assert len(messages) == 9
    assert {m["role"] for m in messages} == {"system"}
    assert [m["content"].split("\n", 1)[0] for m in messages] == [STAGE_RAW_PREFIX] * 9


def test_retained_raw_stage_messages_only_marks_take_a_stage_out_of_raw() -> None:
    stages = [
        _stage("s-named", 1, context_evicted=True),
        _stage("s-closed", 2, context_visible=False),
        _stage("s-plain", 3),
    ]
    messages, retained_ids = retained_raw_stage_messages(_stage_state(stages))
    assert retained_ids == {"s-plain"}
    assert [_payload(m)["stage_id"] for m in messages] == ["s-plain"]


def test_retained_raw_stage_messages_order_follows_stage_index() -> None:
    stages = [
        _stage("s-c", stage_index=5),
        _stage("s-a", stage_index=1),
        _stage("s-b", stage_index=3),
    ]
    messages, _retained = retained_raw_stage_messages(_stage_state(stages))
    assert [json.loads(m["content"].split("\n", 1)[1])["stage_id"] for m in messages] == ["s-a", "s-b", "s-c"]


def test_retained_raw_stage_messages_active_stage_emitted_once() -> None:
    stages = [
        _stage("s1", 1, status="completed"),
        _stage("s2", 2, status="active"),
    ]
    messages, retained_ids = retained_raw_stage_messages(_stage_state(stages, active_stage_id="s2"))
    assert retained_ids == {"s1"}
    assert [_payload(m)["stage_id"] for m in messages] == ["s1", "s2"]


def test_retained_raw_stage_messages_excludes_active_status_case_and_whitespace_insensitive() -> None:
    stages = [
        _stage("s1", 1, status="active"),
        _stage("s2", 2, status=" Active "),  # 带空格 + 大写
        _stage("s3", 3, status="completed"),
        _stage("s4", 4),  # 无 status 视为已完成（默认保留）
    ]
    _messages, retained_ids = retained_raw_stage_messages(_stage_state(stages))
    assert retained_ids == {"s3", "s4"}


def test_retained_raw_stage_messages_excludes_non_normal_stage_kind() -> None:
    stages = [
        _stage("s1", 1, stage_kind="plan"),
        _stage("s2", 2, stage_kind=" nucLEAR "),  # 大写+空格同样排除
        _stage("s3", 3, stage_kind="normal"),
        _stage("s4", 4),  # 缺省 stage_kind → "normal"
        _stage("s5", 5, stage_kind=""),  # 空串 → "normal"
    ]
    _messages, retained_ids = retained_raw_stage_messages(_stage_state(stages))
    assert retained_ids == {"s3", "s4", "s5"}


def test_retained_raw_stage_messages_skips_empty_stage_id() -> None:
    stages = [
        _stage("", 1),
        _stage("   ", 2),
        _stage(None, 3),
        _stage("s4", 4),
    ]
    _messages, retained_ids = retained_raw_stage_messages(_stage_state(stages))
    assert retained_ids == {"s4"}


def test_retained_raw_stage_messages_ignores_non_dict_entries() -> None:
    stages = [_stage("s1", 1), None, "junk", 42, {"no_stage_id": True}]
    _messages, retained_ids = retained_raw_stage_messages(_stage_state(stages))
    assert retained_ids == {"s1"}


@pytest.mark.parametrize(
    "bad_state",
    [None, {}, [], "junk", 42, {"stages": "not-a-list"}, {"stages": [None, "x", 7]}],
    ids=["none", "empty-dict", "empty-list", "string", "int", "stages-string", "stages-junk-entries"],
)
def test_retained_raw_stage_messages_tolerates_invalid_stage_state(bad_state: Any) -> None:
    messages, retained_ids = retained_raw_stage_messages(bad_state)
    assert messages == []
    assert retained_ids == set()


def test_retained_raw_stage_messages_non_normal_active_stage_still_emitted() -> None:
    stages = [
        _stage("s1", 1, status="active"),
        _stage("s2", 2, status="completed", stage_kind="plan"),
    ]
    messages, retained_ids = retained_raw_stage_messages(_stage_state(stages, active_stage_id="s1"))
    assert retained_ids == set()  # plan 阶段不进 retained
    assert len(messages) == 1
    assert _payload(messages[0])["stage_id"] == "s1"


def test_retained_raw_stage_messages_content_contract() -> None:
    stages = [_stage("s1", 1, stage_goal="检查 中文 目标 / quote \"x\"")]
    messages, _retained = retained_raw_stage_messages(_stage_state(stages))
    message = messages[0]
    assert message["role"] == "system"
    content = message["content"]
    assert content.startswith(f"{STAGE_RAW_PREFIX}\n")
    assert STAGE_RAW_PREFIX == "[G3KU_STAGE_RAW_V1]"
    payload_text = content[len(STAGE_RAW_PREFIX) + 1 :]
    assert "\n" not in payload_text  # 单行 JSON，无缩进
    assert "中文" in payload_text  # ensure_ascii=False：CJK 原样保留
    assert "\\u4e2d" not in payload_text
    parsed = json.loads(payload_text)
    assert list(parsed.keys()) == sorted(parsed.keys())  # sort_keys=True
    assert parsed["stage_goal"] == "检查 中文 目标 / quote \"x\""


def test_retained_raw_stage_messages_normalization_of_full_stage() -> None:
    tool = {
        "tool_call_id": "call-1 ",
        "tool_name": " bash ",
        "status": " done ",
        "arguments": {"cmd": "ls"},  # 嵌套 dict 保留
        "arguments_text": " {json} ",
        "output_text": " 保留首尾空格 ",
        "output_preview_text": " prev ",
        "output_ref": "out/1 ",
        "started_at": " t0 ",
        "finished_at": " t1 ",
        "timestamp": " ts ",
        "elapsed_seconds": 1.5,
        "kind": " inline ",
        "source": " agent ",
    }
    scrambled_round = {
        "round_id": " r2 ",
        "round_index": "2",
        "created_at": " c2 ",
        "text": " second ",
        "budget_counted": True,
        "overflow": True,
        "orphan": False,
        "orphan_grafted": False,
        "tool_names": ["cat", "", "  ", "ls"],
        "tool_call_ids": ["c1", "", None],
        "tools": [tool, "not-a-dict", None],
    }
    stage = {
        "stage_index": "3",
        "stage_id": " s3 ",
        "stage_goal": " goal ",
        "preamble_text": " pre ",
        "status": " active ",  # active：不进 retained，仅经 active 路径输出一次
        "stage_kind": " NORMAL ",
        "mode": " 自主 ",
        "system_generated": True,
        "tool_round_budget": "2",
        "tool_rounds_used": "1",
        "completed_stage_summary": " sum ",
        "created_at": " t0 ",
        "finished_at": " t1 ",
        "key_refs": [{"id": "k1", "nested": {"a": 1}}],
        # 故意乱序 rounds：验证按 round_index 排序与字符串 round_index 转 int
        "rounds": [scrambled_round, {"round_id": "r1", "round_index": 1, "text": " first "}],
    }
    messages, _retained = retained_raw_stage_messages(_stage_state([stage], active_stage_id="s3"))
    assert len(messages) == 1
    parsed = _payload(messages[0])
    assert parsed["stage_id"] == "s3"
    assert parsed["stage_index"] == 3
    assert parsed["status"] == "active"
    assert parsed["stage_kind"] == "NORMAL"  # 仅 strip，不归一小写；空则回落 "normal"
    assert parsed["mode"] == "自主"
    assert parsed["system_generated"] is True
    assert parsed["tool_round_budget"] == 2
    assert parsed["tool_rounds_used"] == 1
    assert parsed["rounds"][0]["round_id"] == "r1"
    assert parsed["rounds"][1]["round_id"] == "r2"
    assert parsed["rounds"][1]["round_index"] == 2
    assert parsed["rounds"][0]["tool_names"] == []  # 裸 round 缺省空列表

    round_two = parsed["rounds"][1]
    assert round_two["tool_names"] == ["cat", "ls"]
    assert round_two["tool_call_ids"] == ["c1"]
    assert round_two["budget_counted"] is True
    assert round_two["orphan_grafted"] is False

    normalized_tool = round_two["tools"][0]
    assert normalized_tool == {
        "tool_call_id": "call-1",
        "tool_name": "bash",
        "status": "done",
        "arguments": {"cmd": "ls"},
        "arguments_text": "{json}",
        "output_text": " 保留首尾空格 ",  # output_text 不 strip
        "output_preview_text": "prev",
        "output_ref": "out/1",
        "started_at": "t0",
        "finished_at": "t1",
        "timestamp": "ts",
        "elapsed_seconds": 1.5,
        "kind": "inline",
        "source": "agent",
    }
    assert len(round_two["tools"]) == 1  # 非 dict 工具被过滤
    assert parsed["key_refs"] == [{"id": "k1", "nested": {"a": 1}}]

    # 缺省回落实测：裸字段缺失时的默认值
    bare_stage = {"stage_id": "b1", "stage_index": 1}  # 其余字段全部缺失
    bare = _payload(retained_raw_stage_messages(_stage_state([bare_stage], active_stage_id="b1"))[0][0])
    assert bare["mode"] == ""
    assert bare["stage_kind"] == "normal"  # 空串 stage_kind 回落 "normal"
    assert bare["tool_round_budget"] == 0
    assert bare["tool_rounds_used"] == 0
    assert bare["system_generated"] is False
    assert bare["key_refs"] == []
    assert bare["rounds"] == []


def test_retained_raw_stage_messages_tool_elapsed_seconds_edge_values() -> None:
    def first_tool(elapsed: Any) -> Any:
        tool = {
            "tool_call_id": "c1",
            "tool_name": "x",
            "arguments": {},
            "elapsed_seconds": elapsed,
        }
        message = retained_raw_stage_messages(
            _stage_state([_stage("s1", 1, rounds=[{"round_index": 1, "tools": [tool]}])])
        )[0][0]
        return _payload(message)["rounds"][0]["tools"][0]["elapsed_seconds"]

    assert first_tool(3) == 3.0  # int → float
    assert first_tool(1.5) == 1.5
    assert first_tool(0) == 0.0  # 数值 0：or 0.0 兜底后仍为数值
    assert first_tool(True) == 1.0
    # 非数值一律 None 容错（condition 先判 isinstance，or 兜底不生效）
    assert first_tool("12.3") is None
    assert first_tool(None) is None
    assert first_tool("") is None
    assert first_tool("wrong") is None


def test_retained_raw_stage_messages_rounds_sorted_and_bad_entries_filtered() -> None:
    stage = _stage(
        "s1",
        1,
        rounds=[
            {"round_index": 5, "round_id": "r5"},
            "junk",
            None,
            {"round_id": "r0"},  # 无 round_index → 0，排最前
            {"round_index": 2, "round_id": "r2"},
        ],
    )
    message = retained_raw_stage_messages(_stage_state([stage]))[0][0]
    rounds = _payload(message)["rounds"]
    assert [r["round_id"] for r in rounds] == ["r0", "r2", "r5"]
    assert [r["round_index"] for r in rounds] == [0, 2, 5]


def test_retained_raw_stage_messages_is_deterministic_and_does_not_mutate_input() -> None:
    stages = [
        _stage("s1", 1, status="completed", rounds=[{"round_index": 2, "round_id": "r2"}, {"round_index": 1, "round_id": "r1"}]),
        _stage("s2", 2, status="active"),
    ]
    state = _stage_state(stages, active_stage_id="s2")
    snapshot = copy.deepcopy(state)

    messages_a, ids_a = retained_raw_stage_messages(state)
    messages_b, ids_b = retained_raw_stage_messages(state)

    assert messages_a == messages_b
    assert ids_a == ids_b
    assert state == snapshot  # 输入未被原地改写
    # 输出内部无共享引用：改动 messages_a 不影响 messages_b
    messages_a[0]["content"] = "mutated"
    assert messages_b[0]["content"] != "mutated"


def test_retained_raw_stage_messages_duplicate_stage_id_last_wins() -> None:
    stages = [
        _stage("dup", 1, status="completed", stage_goal="old"),
        _stage("dup", 2, status="completed", stage_goal="new"),
    ]
    messages, retained_ids = retained_raw_stage_messages(_stage_state(stages))
    assert retained_ids == {"dup"}
    assert len(messages) == 1
    assert _payload(messages[0])["stage_goal"] == "new"
    assert _payload(messages[0])["stage_index"] == 2


def test_retained_raw_stage_messages_active_stage_pointing_at_completed_stage_emits_once() -> None:
    # active_stage_id 指向一条 status != "active" 的阶段时，共享判据按 active_id 排除它，
    # 本模块只在 active 路径输出一次 → 不再出现同 stage_id 的两个 raw 块。
    stages = [
        _stage("s1", 1, status="completed"),
        _stage("s2", 2, status="completed"),  # 非 active 却被 active_stage_id 引用
    ]
    messages, retained_ids = retained_raw_stage_messages(_stage_state(stages, active_stage_id="s2"))
    assert retained_ids == {"s1"}
    assert [_payload(m)["stage_id"] for m in messages] == ["s1", "s2"]


def test_retained_raw_stage_messages_object_stage_entries_are_not_rendered() -> None:
    # _stage_list 只收 dict 条目：对象条目在 getattr 路径不进入渲染
    class _StageObj:
        stage_id = "obj-stage"
        stage_index = 9

        @property
        def stages(self) -> list[Any]:  # pragma: no cover - 占位
            return []

    state = SimpleNamespace(active_stage_id="", stages=[_StageObj()])
    messages, retained_ids = retained_raw_stage_messages(state)
    assert messages == []
    assert retained_ids == set()
