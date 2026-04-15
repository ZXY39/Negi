from __future__ import annotations

import sys
import types
from types import SimpleNamespace

if "litellm" not in sys.modules:
    litellm_stub = types.ModuleType("litellm")

    async def _unreachable_acompletion(*args, **kwargs):
        raise AssertionError("litellm acompletion should not be used in websocket_ceo tests")

    litellm_stub.acompletion = _unreachable_acompletion
    litellm_stub.api_base = None
    litellm_stub.suppress_debug_info = True
    litellm_stub.drop_params = True
    sys.modules["litellm"] = litellm_stub

from g3ku.runtime.api import websocket_ceo


def test_resolve_final_execution_trace_summary_keeps_current_turn_snapshot_summary() -> None:
    current_summary = {
        "stages": [
            {
                "stage_id": "frontdoor-stage-current",
                "stage_goal": "inspect repository",
                "rounds": [],
            }
        ]
    }
    persisted_session = SimpleNamespace(
        messages=[
            {
                "role": "assistant",
                "content": "older reply",
                "execution_trace_summary": {
                    "stages": [{"stage_id": "frontdoor-stage-old", "stage_goal": "old stage", "rounds": []}]
                },
            }
        ]
    )
    session = SimpleNamespace(
        _frontdoor_execution_trace_summary_snapshot=lambda: current_summary,
    )

    summary = websocket_ceo._resolve_final_execution_trace_summary(
        payload={"text": "done"},
        session=session,
        persisted_session=persisted_session,
    )

    assert summary == current_summary


def test_resolve_final_execution_trace_summary_does_not_reuse_previous_assistant_trace() -> None:
    persisted_session = SimpleNamespace(
        messages=[
            {
                "role": "assistant",
                "content": "older reply",
                "execution_trace_summary": {
                    "stages": [{"stage_id": "frontdoor-stage-old", "stage_goal": "old stage", "rounds": []}]
                },
            }
        ]
    )

    summary = websocket_ceo._resolve_final_execution_trace_summary(
        payload={"text": "direct reply without stage"},
        session=SimpleNamespace(),
        persisted_session=persisted_session,
    )

    assert summary == {}
