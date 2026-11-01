"""静默的整轮范围与渠道不投递：文案落点 + 被压正文读数。

实盘一次用户轮里，模型先给出 295 字正文、再用 `silent` 收尾，理由写的是「本轮已通过正文
说明」——渠道端只在收尾那一跳投递一次，那句正文从来没到过用户眼前，Web 端也只是整轮折成
一行。这里钉四件事：模型可见的 `silent` 契约讲清"整轮"范围与两条车道的可见性、前门回执
交出运行时才知道的被压字数、节点侧拿不到读数就不带该字段、前门提示词不再宣称「已展示的
正文仍保留」。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from g3ku.json_schema_utils import normalize_openai_tool_definition
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from main.runtime.internal_tools import SilentTool

FRONTDOOR_PROMPT = (
    Path(__file__).resolve().parents[2] / "g3ku" / "runtime" / "prompts" / "ceo_frontdoor.md"
)


def _run(tool: SilentTool, **kwargs) -> dict:
    return json.loads(asyncio.run(tool.execute(**kwargs)))


def test_model_visible_silent_contract_states_whole_turn_scope() -> None:
    """前门取的是 `model_description`（`_model_visible_tool_contract`），所以判据必须在那一层。"""
    function = normalize_openai_tool_definition(SilentTool().to_model_schema())["function"]
    description = str(function.get("description") or "")

    assert "nothing delivered" in description
    assert "whole turn" in description
    # 两条车道的可见性都要写进去，否则模型按 Web 的可见性推断渠道。
    assert "closing hop" in description
    assert "collapsed line" in description
    assert "waiting on an answer, reply normally" in description
    # 模型可见文案全英文。
    assert description.isascii()
    # 旧句子把"留在上下文"写成"已经交付"，正是那次误判的措辞来源。
    assert "keep the text for context, deliver nothing" not in description


def test_silent_receipt_carries_undelivered_turn_text_chars() -> None:
    payload = _run(
        SilentTool(pending_reply_chars=lambda: 295),
        reason="no new user-visible information",
    )
    assert payload["silenced"] is True
    assert payload["undelivered_text_chars"] == 295

    assert "undelivered_text_chars" not in _run(SilentTool(), reason="heartbeat done")
    assert "undelivered_text_chars" not in _run(SilentTool(pending_reply_chars=lambda: 0), reason="r")

    def _boom() -> int:
        raise RuntimeError("ledger unreadable")

    assert "undelivered_text_chars" not in _run(SilentTool(pending_reply_chars=_boom), reason="r")


def _ops() -> CeoFrontDoorRuntimeOps:
    return object.__new__(CeoFrontDoorRuntimeOps)


def _runtime(rows: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(_frontdoor_turns=rows)))


def test_frontdoor_counts_prior_hops_and_the_current_hop() -> None:
    rows = [
        {"seq": 1, "turn_id": "turn-a", "text": "第一段正文" * 5, "tools": []},
        {"seq": 2, "turn_id": "turn-a", "text": "", "tools": []},
        {"seq": 3, "turn_id": "turn-b", "text": "别的回合不该算进来" * 4, "tools": []},
    ]
    prior = len(rows[0]["text"])
    state = {
        "frontdoor_turn_id": "turn-a",
        "analysis_text": "随工具一起给出的这一段",
    }

    counted = _ops()._frontdoor_undelivered_turn_text_chars(state=state, runtime=_runtime(rows))
    assert counted == prior + len(state["analysis_text"])

    # 回合账本只有当前轮的行；上一轮的正文不计入本轮被压量。
    other_turn = _ops()._frontdoor_undelivered_turn_text_chars(
        state={"frontdoor_turn_id": "turn-b", "analysis_text": ""},
        runtime=_runtime(rows),
    )
    assert other_turn == len(rows[2]["text"])

    # 拿不到 turn id / 没有会话对象时读数缺席，而不是报 0 之外的事实。
    assert _ops()._frontdoor_undelivered_turn_text_chars(state={}, runtime=_runtime(rows)) == 0
    assert (
        _ops()._frontdoor_undelivered_turn_text_chars(
            state={"frontdoor_turn_id": "turn-a", "analysis_text": "x"},
            runtime=None,
        )
        == 1
    )


def test_frontdoor_resident_silent_carries_the_reading_provider() -> None:
    """接线本身：常驻 `silent` 实例必须带读数提供者，否则回执永远缺该字段。"""
    resident = _ops()._frontdoor_resident_tools(
        state={"frontdoor_turn_id": "turn-a", "analysis_text": ""},
        runtime=_runtime([]),
    )
    tool = resident["silent"]
    assert isinstance(tool, SilentTool)
    assert callable(tool._pending_reply_chars)
    assert "undelivered_text_chars" not in _run(tool, reason="r")

    read = _ops()._frontdoor_resident_tools(
        state={"frontdoor_turn_id": "turn-a", "analysis_text": ""},
        runtime=_runtime([{"seq": 1, "turn_id": "turn-a", "text": "已经给出的正文", "tools": []}]),
    )["silent"]
    assert _run(read, reason="r")["undelivered_text_chars"] == len("已经给出的正文")


def test_frontdoor_prompt_replaces_the_already_displayed_claim() -> None:
    prompt = FRONTDOOR_PROMPT.read_text(encoding="utf-8")

    assert "期间已展示的正文与工具调用仍保留" not in prompt
    assert "渠道端只在收尾那一跳投递一次" in prompt
    assert "整轮折成一行「静默消息」" in prompt
    assert "`reason` 只落进转录与后续轮次，不会显示给用户" in prompt
