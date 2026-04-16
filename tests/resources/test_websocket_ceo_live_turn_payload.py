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


def test_build_live_turn_payload_omits_preserved_turn_when_assistant_turn_already_persisted() -> None:
    session = SimpleNamespace(
        inflight_turn_snapshot=lambda: {
            "source": "heartbeat",
            "turn_id": "turn-heartbeat-current",
            "status": "running",
            "assistant_text": "heartbeat processing",
        },
        preserved_inflight_turn_snapshot=lambda: {
            "source": "user",
            "turn_id": "turn-user-preserved",
            "status": "running",
            "user_message": {"content": "Install the skill"},
            "assistant_text": "Still working on it...",
        },
    )
    persisted_session = SimpleNamespace(
        messages=[
            {
                "role": "assistant",
                "turn_id": "turn-user-preserved",
                "content": "Already persisted final reply",
            }
        ]
    )

    payload = websocket_ceo._build_live_turn_payload(session, "web:shared", persisted_session)

    assert payload["inflight_turn"]["turn_id"] == "turn-heartbeat-current"
    assert "preserved_turn" not in payload


def test_build_live_turn_payload_keeps_preserved_turn_when_assistant_turn_not_persisted() -> None:
    session = SimpleNamespace(
        inflight_turn_snapshot=lambda: {
            "source": "heartbeat",
            "turn_id": "turn-heartbeat-current",
            "status": "running",
            "assistant_text": "heartbeat processing",
        },
        preserved_inflight_turn_snapshot=lambda: {
            "source": "user",
            "turn_id": "turn-user-preserved",
            "status": "running",
            "user_message": {"content": "Install the skill"},
            "assistant_text": "Still working on it...",
        },
    )
    persisted_session = SimpleNamespace(messages=[])

    payload = websocket_ceo._build_live_turn_payload(session, "web:shared", persisted_session)

    assert payload["inflight_turn"]["turn_id"] == "turn-heartbeat-current"
    assert payload["preserved_turn"]["turn_id"] == "turn-user-preserved"
