from __future__ import annotations

from g3ku.runtime.frontdoor.canonical_context import (
    canonical_context_delta,
    merge_turn_stage_state_into_canonical_context,
    normalize_frontdoor_canonical_context,
    project_canonical_context_for_ui_payload,
    project_canonical_context_for_transcript,
    ui_canonical_context_delta,
)


def _stage(
    stage_id: str,
    index: int,
    *,
    status: str = "completed",
    goal: str = "",
    summary: str = "",
    rounds: list[dict[str, object]] | None = None,
    representation: str | None = None,
) -> dict[str, object]:
    return {
        "stage_id": stage_id,
        "stage_index": index,
        "stage_goal": goal or f"goal {index}",
        "completed_stage_summary": summary or f"summary {index}",
        "status": status,
        "stage_kind": "normal",
        "representation": representation,
        "created_at": f"2026-09-01T00:0{index}:00+08:00",
        "finished_at": f"2026-09-01T00:1{index}:00+08:00",
        "rounds": list(rounds or []),
    }


def _tool(name: str, output_text: str = "", **overrides: object) -> dict[str, object]:
    return {
        "tool_call_id": f"{name}:1",
        "tool_name": name,
        "status": "success",
        "arguments": {"path": "a"},
        "arguments_text": "read a",
        "output_text": output_text,
        "output_preview_text": output_text[:80],
        "output_ref": f"artifact://{name}",
        **overrides,
    }


def test_normalize_keeps_latest_copy_for_repeated_stage_id() -> None:
    normalized = normalize_frontdoor_canonical_context(
        {
            "stages": [
                _stage("frontdoor-stage-1", 1, summary="old"),
                _stage("frontdoor-stage-1", 1, summary="new"),
            ]
        }
    )

    assert len(normalized["stages"]) == 1
    assert normalized["stages"][0]["completed_stage_summary"] == "new"


def test_normalize_collapses_rebased_copies_of_the_same_completed_stage() -> None:
    original = _stage("frontdoor-stage-1", 1, summary="same")
    rebased = dict(original)
    rebased["stage_id"] = "frontdoor-stage-98"
    rebased["stage_index"] = 98

    normalized = normalize_frontdoor_canonical_context({"stages": [original, rebased]})

    assert [stage["stage_id"] for stage in normalized["stages"]] == ["frontdoor-stage-98"]


def test_repeated_finalization_does_not_reappend_the_carried_workset() -> None:
    durable = normalize_frontdoor_canonical_context(
        {"stages": [_stage(f"frontdoor-stage-{index}", index) for index in range(1, 96)]}
    )
    turn_state = {
        "active_stage_id": "frontdoor-stage-97",
        "stages": [
            *[dict(stage) for stage in durable["stages"]],
            _stage("frontdoor-stage-96", 96, goal="new work", summary="new result"),
            _stage("frontdoor-stage-97", 97, status="active", goal="current", summary=""),
        ],
    }

    first = merge_turn_stage_state_into_canonical_context(durable, turn_state)
    second = merge_turn_stage_state_into_canonical_context(first, turn_state)

    stage_ids = [str(stage.get("stage_id")) for stage in first["stages"]]
    assert len(stage_ids) == len(set(stage_ids))
    assert len(first["stages"]) < 100
    assert len(second["stages"]) <= len(first["stages"]) + 1


def test_transcript_projection_compacts_old_stages_and_caps_tool_bodies() -> None:
    long_output = "x" * 2500
    long_arguments = {"payload": "y" * 3000}
    context = {
        "active_stage_id": "",
        "stages": [
            _stage(
                "frontdoor-stage-1",
                1,
                rounds=[
                    {
                        "round_index": 1,
                        "text": "old round",
                        "tools": [_tool("old", output_text=long_output, arguments=long_arguments)],
                    }
                ],
            ),
            *[
                _stage(
                    f"frontdoor-stage-{index}",
                    index,
                    rounds=[
                        {
                            "round_index": 1,
                            "text": "kept round",
                            "tools": [_tool(f"kept-{index}", output_text=long_output)],
                        }
                    ],
                )
                for index in range(2, 6)
            ],
        ]
    }

    projected = project_canonical_context_for_transcript(context)
    old_stage = projected["stages"][0]
    kept_stage = projected["stages"][-1]
    kept_tool = kept_stage["rounds"][0]["tools"][0]

    assert old_stage["representation"] == "compact"
    assert old_stage["rounds"] == []
    assert kept_stage["representation"] == "raw"
    assert len(kept_tool["output_text"]) == 0
    assert kept_tool["output_preview_text"] == long_output[:80]
    assert kept_tool["output_ref"] == "artifact://kept-5"


def test_transcript_projection_caps_oversized_tool_arguments() -> None:
    projected = project_canonical_context_for_transcript(
        {
            "stages": [
                _stage(
                    "frontdoor-stage-9",
                    9,
                    rounds=[
                        {
                            "round_index": 1,
                            "tools": [_tool("large", arguments={"payload": "y" * 3000})],
                        }
                    ],
                )
            ]
        }
    )
    tool = projected["stages"][0]["rounds"][0]["tools"][0]

    assert tool["arguments"] == {}
    assert len(str(tool["arguments_text"])) <= 4000


def test_ui_delta_ignores_projection_representation_flips() -> None:
    rounds = [
        {
            "round_index": 1,
            "text": f"same round {index}",
            "tools": [_tool(f"read-{index}", output_text="same output")],
        }
        for index in range(1, 6)
    ]
    persisted = normalize_frontdoor_canonical_context(
        {
            "stages": [
                _stage(f"frontdoor-stage-{index}", index, rounds=[rounds[index - 1]])
                for index in range(1, 6)
            ]
        }
    )
    persisted_projected = project_canonical_context_for_transcript(persisted)
    live = {
        "active_stage_id": "",
        "stages": [
            _stage(
                f"frontdoor-stage-{index}",
                index,
                representation="raw",
                rounds=[rounds[index - 1]],
            )
            for index in range(1, 6)
        ],
    }

    raw_delta = canonical_context_delta(persisted_projected, live)
    ui_delta = ui_canonical_context_delta(persisted_projected, live)

    assert len(raw_delta.get("stages") or []) >= 2
    assert ui_delta == {}


def test_ui_delta_keeps_only_new_stages_and_backfills_live_bodies() -> None:
    old_rounds = [
        {
            "round_index": 1,
            "text": f"old round {index}",
            "tools": [
                _tool(
                    f"old-{index}",
                    output_text="x" * 3000 if index >= 4 else "old output",
                    arguments_text="q" * 6000 if index == 5 else "arg",
                )
            ],
        }
        for index in range(1, 6)
    ]
    new_round = {
        "round_index": 1,
        "text": "y" * 6000,
        "tools": [
            _tool(
                "new",
                output_text="z" * 3000,
                arguments_text="q" * 6000,
            )
        ],
    }
    persisted_stages = [
        _stage(f"frontdoor-stage-{index}", index, rounds=[old_rounds[index - 1]])
        for index in range(1, 6)
    ]
    persisted_projected = project_canonical_context_for_transcript(
        {
            "stages": [dict(stage) for stage in persisted_stages]
        }
    )
    live = {
        "active_stage_id": "",
        "stages": [
            _stage(
                f"frontdoor-stage-{index}",
                index,
                representation="raw",
                rounds=[old_rounds[index - 1]],
            )
            for index in range(1, 6)
        ]
        + [_stage("frontdoor-stage-6", 6, representation="raw", rounds=[new_round])],
    }

    ui_delta = ui_canonical_context_delta(persisted_projected, live)
    delta_stages = list(ui_delta.get("stages") or [])

    assert [stage["stage_id"] for stage in delta_stages] == ["frontdoor-stage-6"]
    rendered_round = delta_stages[0]["rounds"][0]
    rendered_tool = rendered_round["tools"][0]
    assert rendered_round["text"] == "y" * 6000
    assert rendered_tool["output_text"] == "z" * 3000
    assert rendered_tool["arguments_text"] == "q" * 6000


def test_ui_payload_projection_keeps_window_bodies_bounded() -> None:
    context = {
        "stages": [
            _stage("frontdoor-stage-1", 1, rounds=[{"round_index": 1, "tools": [_tool("old")]}]),
            _stage("frontdoor-stage-2", 2, rounds=[{"round_index": 1, "tools": [_tool("old-2")]}]),
            _stage("frontdoor-stage-3", 3, rounds=[{"round_index": 1, "tools": [_tool("old-3")]}]),
            _stage(
                "frontdoor-stage-4",
                4,
                rounds=[
                    {
                        "round_index": 1,
                        "tools": [_tool("kept", output_text="x" * 3000)],
                    }
                ],
            ),
            _stage(
                "frontdoor-stage-5",
                5,
                rounds=[
                    {
                        "round_index": 1,
                        "tools": [_tool("kept-5", output_text="x" * 3000)],
                    }
                ],
            ),
        ]
    }

    projected = project_canonical_context_for_ui_payload(context)

    assert projected["stages"][0]["representation"] == "compact"
    assert projected["stages"][0]["rounds"] == []
    assert projected["stages"][-1]["representation"] == "raw"
    assert projected["stages"][-1]["rounds"][0]["tools"][0]["output_text"] == "x" * 3000


def test_transcript_projection_returns_empty_for_missing_stage_state() -> None:
    assert project_canonical_context_for_transcript({}) == {}
    assert project_canonical_context_for_transcript({"stages": []}) == {}
