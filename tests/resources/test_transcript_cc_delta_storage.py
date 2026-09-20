from __future__ import annotations

import json
from types import SimpleNamespace

from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.api import websocket_ceo
from g3ku.runtime.frontdoor.canonical_context import (
    TRANSCRIPT_PROJECTION_MODE,
    apply_cc_upsert,
    encode_cc_upsert,
    materialize_transcript_view,
    project_canonical_context_for_transcript,
)
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder


def _stage(index: int, *, summary: str = "", rounds: list[dict] | None = None) -> dict:
    return {
        "stage_id": f"frontdoor-stage-{index}",
        "stage_index": index,
        "stage_goal": f"goal {index}",
        "completed_stage_summary": summary or f"summary {index}",
        "status": "completed",
        "stage_kind": "normal",
        "rounds": rounds if rounds is not None else [
            {
                "round_index": 1,
                "tools": [
                    {
                        "tool_call_id": f"t{index}:1",
                        "tool_name": f"tool{index}",
                        "status": "success",
                    }
                ],
            }
        ],
    }


def _view(stage_count: int, *, revise_last: bool = False) -> dict:
    stages = [
        _stage(index, summary=f"summary {index} revised" if (revise_last and index == stage_count) else "")
        for index in range(1, stage_count + 1)
    ]
    return project_canonical_context_for_transcript({"active_stage_id": "", "stages": stages})


def _checkpoint_row(content: str, view: dict, **extra) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "canonical_context": view,
        "canonical_context_projection": TRANSCRIPT_PROJECTION_MODE,
        **extra,
    }


def _delta_row(content: str, payload: dict, **extra) -> dict:
    row = {
        "role": "assistant",
        "content": content,
        "cc_upsert": payload,
        "canonical_context_projection": "delta_window",
        **extra,
    }
    return row


def test_encode_apply_roundtrip_on_prefix_growth_and_tail_revision() -> None:
    v4 = _view(4)
    v5 = _view(5)
    payload = encode_cc_upsert(v4, v5)
    assert payload is not None
    # 新增 stage-5 同时窗口外移：stage-2 在上一视图里还是 raw，本视图已是 compact，
    # 属于真实 revise，必须进 upsert（否则重放出的视图表示层与存量不一致）。
    assert [stage["stage_id"] for stage in payload["upsert"]] == ["frontdoor-stage-2", "frontdoor-stage-5"]
    assert apply_cc_upsert(v4, payload) == v5

    # 尾部 revise（active 定稿 / raw→compact 单向窗口移动）走原位替换。
    v5r = _view(5, revise_last=True)
    payload = encode_cc_upsert(v5, v5r)
    assert payload is not None
    assert [stage["stage_id"] for stage in payload["upsert"]] == ["frontdoor-stage-5"]
    assert apply_cc_upsert(v5, payload) == v5r

    # 无变化 → 空 upsert，重放仍恒等。
    payload = encode_cc_upsert(v5r, v5r)
    assert payload == {"upsert": []}
    assert apply_cc_upsert(v5r, payload) == v5r


def test_encode_refuses_unreducible_reorder() -> None:
    v5 = _view(5)
    reordered = {**v5, "stages": list(reversed([dict(stage) for stage in v5["stages"]]))}
    assert encode_cc_upsert(v5, reordered) is None


def test_snapshot_rows_match_full_storage_when_tail_is_delta_encoded() -> None:
    v1, v2, v3 = _view(2), _view(4), _view(6, revise_last=True)
    d12 = encode_cc_upsert(v1, v2)
    d23 = encode_cc_upsert(v2, v3)
    assert d12 is not None and d23 is not None

    full_rows = [
        _checkpoint_row("r1", v1),
        _checkpoint_row("r2", v2),
        _checkpoint_row("r3", v3),
    ]
    delta_rows = [
        _checkpoint_row("r1", v1),
        _delta_row("r2", d12),
        _delta_row("r3", d23),
    ]
    assert websocket_ceo._build_ceo_snapshot(full_rows) == websocket_ceo._build_ceo_snapshot(delta_rows)


def test_snapshot_delta_chain_replays_through_hidden_runtime_rows() -> None:
    v1 = _view(2)
    v2 = _view(4)
    v3 = _view(6, revise_last=True)
    d23 = encode_cc_upsert(v2, v3)
    assert d23 is not None

    full_rows = [
        _checkpoint_row("visible 1", v1),
        _checkpoint_row(
            "hidden runtime",
            v2,
            metadata={"ui_visible": False, "prompt_visible": False, "source": "heartbeat"},
        ),
        _checkpoint_row("visible 2", v3),
    ]
    delta_rows = [
        full_rows[0],
        full_rows[1],
        _delta_row("visible 2", d23),
    ]
    items = websocket_ceo._build_ceo_snapshot(delta_rows)
    assert items == websocket_ceo._build_ceo_snapshot(full_rows)
    # 隐藏行不出帧但必须推进存储游标：删掉这一行会让 r3 的重放基线错位。
    broken = [full_rows[0], delta_rows[2]]
    assert items != websocket_ceo._build_ceo_snapshot(broken)


def test_materialize_transcript_view_three_states() -> None:
    v1, v2, v3 = _view(2), _view(4), _view(6)
    d12 = encode_cc_upsert(v1, v2)
    d23 = encode_cc_upsert(v2, v3)
    assert d12 is not None and d23 is not None
    raw_legacy = {"role": "assistant", "content": "x", "canonical_context": {"stages": [_stage(1)]}}
    rows = [
        _checkpoint_row("r1", v1),
        _delta_row("r2", d12),
        _delta_row("r3", d23),
        raw_legacy,
        {"role": "user", "content": "no rail"},
    ]
    assert materialize_transcript_view(rows, 0) == v1
    assert materialize_transcript_view(rows, 1) == v2
    assert materialize_transcript_view(rows, 2) == v3
    # 全量行原样返回（prompt 文本对 legacy 行保持逐字节不变）；非轨道行返回空。
    assert materialize_transcript_view(rows, 3) == raw_legacy["canonical_context"]
    assert materialize_transcript_view(rows, 4) == {}
    assert materialize_transcript_view([_delta_row("orphan", d12)], 0) == {}


def test_hidden_internal_summary_prompt_text_survives_delta_storage() -> None:
    v1, v2 = _view(3), _view(5)
    d12 = encode_cc_upsert(v1, v2)
    assert d12 is not None
    meta = {"source": "heartbeat", "prompt_visible": False, "ui_visible": False}
    full_rows = [_checkpoint_row("hb done", v2, metadata=dict(meta))]
    delta_rows = [
        _checkpoint_row("r1", v1),
        _delta_row("hb done", d12, metadata=dict(meta)),
    ]

    def _summary(rows: list[dict]) -> list[dict]:
        session = SimpleNamespace(messages=rows)
        return CeoMessageBuilder._hidden_internal_summary_messages(
            persisted_session=session,
            checkpoint_messages=None,
        )

    assert _summary(delta_rows) == _summary(full_rows)


def test_latest_assistant_canonical_context_materializes_delta_rows() -> None:
    v1, v2 = _view(2), _view(4, revise_last=True)
    d12 = encode_cc_upsert(v1, v2)
    assert d12 is not None
    session = SimpleNamespace(
        messages=[
            _checkpoint_row("r1", v1, turn_id="t1"),
            _delta_row("r2", d12, turn_id="t2"),
            _checkpoint_row("r3", v2, turn_id="t3"),
        ]
    )
    # 尾行 checkpoint：直接返回存量视图。
    assert web_ceo_sessions.latest_assistant_message_canonical_context(session) == v2
    # 排除尾行后命中 delta 行：返回其物化视图，与全量存储同形。
    assert (
        web_ceo_sessions.latest_assistant_message_canonical_context(session, exclude_turn_id="t3")
        == v2
    )
    full_equivalent = SimpleNamespace(
        messages=[
            _checkpoint_row("r1", v1, turn_id="t1"),
            _checkpoint_row("r2", v2, turn_id="t2"),
            _checkpoint_row("r3", v2, turn_id="t3"),
        ]
    )
    assert (
        web_ceo_sessions.latest_assistant_message_canonical_context(full_equivalent, exclude_turn_id="t3")
        == web_ceo_sessions.latest_assistant_message_canonical_context(session, exclude_turn_id="t3")
    )


def test_view_json_payload_is_transportable() -> None:
    v4, v6 = _view(4), _view(6)
    payload = encode_cc_upsert(v4, v6)
    assert payload is not None
    wire = json.loads(json.dumps(payload, ensure_ascii=False))
    assert apply_cc_upsert(v4, wire) == v6
