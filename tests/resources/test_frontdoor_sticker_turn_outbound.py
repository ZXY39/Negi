from __future__ import annotations

from types import SimpleNamespace

from g3ku.runtime.frontdoor.turns import append_frontdoor_turn
from g3ku.runtime.session_agent import RuntimeAgentSession


def _session(*turns: tuple[str, str, list[dict]]) -> SimpleNamespace:
    ledger: list[dict] = []
    for turn_id, text, tools in turns:
        ledger = append_frontdoor_turn(ledger, turn_id=turn_id, text=text, tools=tools)
    return SimpleNamespace(_frontdoor_turns=ledger)


def _tool(name: str, *, status: str = "success") -> dict:
    return {"tool_call_id": f"call:{name}", "tool_name": name, "status": status, "arguments_text": "{}"}


def _prefix(session: SimpleNamespace, text: str, turn_id: str) -> str:
    return RuntimeAgentSession._sticker_sheet_turn_text_prefix(session, text, turn_id)


def test_sticker_only_turn_text_is_prepended_to_the_outbound_reply() -> None:
    session = _session(
        ("turn-1", "先找找看", [_tool("exec")]),
        ("turn-1", "找到了，这组表情挺全", [_tool("show_sticker_category")]),
    )

    assert _prefix(session, "那给你发这张～[[sticker:p/h/1]]", "turn-1") == (
        "找到了，这组表情挺全\n\n那给你发这张～[[sticker:p/h/1]]"
    )


def test_a_mixed_turn_keeps_its_text_in_the_rail_only() -> None:
    session = _session(
        ("turn-1", "边看边写", [_tool("show_sticker_category"), _tool("exec")]),
    )

    assert _prefix(session, "最终答复", "turn-1") == "最终答复"


def test_a_failed_sticker_call_does_not_move_text_outbound() -> None:
    session = _session(
        ("turn-1", "图打不开", [_tool("show_sticker_category", status="error")]),
    )

    assert _prefix(session, "最终答复", "turn-1") == "最终答复"


def test_another_turns_sticker_text_is_not_reused() -> None:
    session = _session(
        ("turn-old", "上一轮的正文", [_tool("show_sticker_category")]),
        ("turn-now", "", [_tool("exec")]),
    )

    assert _prefix(session, "最终答复", "turn-now") == "最终答复"


def test_the_prefix_is_not_duplicated_when_the_final_text_already_has_it() -> None:
    session = _session(
        ("turn-1", "找到了，这组表情挺全", [_tool("show_sticker_category")]),
    )

    assert _prefix(session, "找到了，这组表情挺全\n\n最终答复", "turn-1") == (
        "找到了，这组表情挺全\n\n最终答复"
    )


def test_empty_text_or_turn_id_leaves_the_reply_untouched() -> None:
    session = _session(
        ("turn-1", "找到了", [_tool("show_sticker_category")]),
    )

    assert _prefix(session, "", "turn-1") == ""
    assert _prefix(session, "最终答复", " ") == "最终答复"
