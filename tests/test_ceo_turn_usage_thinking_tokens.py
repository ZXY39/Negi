"""provider 上报的思考 token 要能穿过回合 usage 走到转录行。

`web-and-admin.md`「Live Reasoning Row Contract」承诺：历史气泡那一行只有真值到手才去掉
「~」，真值取自 `turn_usage.thinking_tokens`。这条通道上有两处按字段重建 dict 的地方——
回合 usage 的读出白名单、以及请求工件按 turn_id 聚合的那份（续跑轮由它认领累计器）——两处
都把 `thinking_tokens` 摘掉，所以 `thinking.tokens` 恒缺、那一行永远带估算标记。

不变量：provider 没上报时保持缺键。缺键与 0 的区分就是「~」的判据，补成 0 会把它洗成假真值。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.session_agent import RuntimeAgentSession


class _UsageStub:
    """只带累计器的最小宿主：读口是纯函数，不需要会话对象。"""

    def __init__(self, usage: dict[str, dict[str, int]]) -> None:
        self._frontdoor_turn_usage = usage


_UsageStub._turn_usage_payload = RuntimeAgentSession._turn_usage_payload


def test_reported_thinking_tokens_reach_turn_usage_payload() -> None:
    stub = _UsageStub(
        {
            "t1": {
                "input_tokens": 900,
                "output_tokens": 300,
                "cache_hit_tokens": 120,
                "call_count": 2,
                "thinking_tokens": 2591,
            }
        }
    )
    payload = stub._turn_usage_payload("t1")
    assert payload == {
        "input_tokens": 900,
        "output_tokens": 300,
        "cache_hit_tokens": 120,
        "call_count": 2,
        "thinking_tokens": 2591,
    }


def test_unreported_thinking_tokens_stay_absent() -> None:
    stub = _UsageStub(
        {"t1": {"input_tokens": 900, "output_tokens": 300, "cache_hit_tokens": 0, "call_count": 1}}
    )
    payload = stub._turn_usage_payload("t1")
    assert "thinking_tokens" not in payload, payload

    # 上报过 0 是真值的一种，仍要带出去；判「去不去 ~」的是 >0，不是键在不在。
    zero_stub = _UsageStub(
        {"t1": {"input_tokens": 900, "output_tokens": 300, "cache_hit_tokens": 0, "call_count": 1, "thinking_tokens": 0}}
    )
    assert zero_stub._turn_usage_payload("t1")["thinking_tokens"] == 0


def _write_artifacts(directory: Path, rows: list[dict[str, object]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(rows, start=1):
        (directory / f"{index:06d}.json").write_text(json.dumps(row), encoding="utf-8")


def test_artifact_aggregation_keeps_thinking_tokens_per_turn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = root / "first"
        second = root / "second"
        _write_artifacts(
            first,
            [
                {"turn_id": "t1", "usage": {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 500}},
                {"turn_id": "t1", "usage": {"input_tokens": 80, "output_tokens": 30}},
                {"turn_id": "t2", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ],
        )
        _write_artifacts(
            second,
            [{"turn_id": "t3", "usage": {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 7}}],
        )

        with mock.patch.object(
            web_ceo_sessions,
            "actual_request_dir_for_session",
            side_effect=lambda session_id, create=False: {
                "web:ceo-a": first,
                "web:ceo-b": second,
            }.get(str(session_id).strip()),
        ):
            by_session = {
                key: web_ceo_sessions.read_session_turn_token_usage(key)
                for key in ("web:ceo-a", "web:ceo-b")
            }

        turns_a = by_session["web:ceo-a"]
        assert turns_a["t1"]["thinking_tokens"] == 500, turns_a["t1"]
        assert turns_a["t1"]["call_count"] == 2, turns_a["t1"]
        # 那轮一跳都没上报：键必须不存在，续跑认领时才不会被补成 0。
        assert "thinking_tokens" not in turns_a["t2"], turns_a["t2"]
        assert by_session["web:ceo-b"]["t3"]["thinking_tokens"] == 7
