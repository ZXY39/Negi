"""E2E mock-bridge verification for the External Agent API (/api/v1).

Runs against a live g3ku web instance with externalApi enabled:

    python scripts/e2e_external_api_mock_bridge.py \
        --base-url http://127.0.0.1:18790 --token <externalApi token>

Exercises the contract a real bridge relies on: Bearer auth, idempotent
session get-or-create, async message submission, the SSE event stream
(including the exactly-one-terminal-event invariant), Idempotency-Key
dedup, Last-Event-ID replay, state snapshot, and clear semantics.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

import httpx

TIMEOUT = 180.0


def _fail(message: str) -> None:
    print(f"[FAIL] {message}")
    raise SystemExit(1)


async def _consume_until_terminal(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    last_event_id: int | None = None,
    timeout: float = TIMEOUT,
) -> list[dict]:
    events: list[dict] = []
    headers = {}
    if last_event_id:
        headers["Last-Event-ID"] = str(last_event_id)
    async with asyncio.timeout(timeout):
        async with client.stream("GET", f"/api/v1/sessions/{session_id}/events", headers=headers) as response:
            response.raise_for_status()
            data_buffer: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_buffer.append(line[5:].strip())
                elif line == "" and data_buffer:
                    import json

                    event = json.loads("\n".join(data_buffer))
                    data_buffer = []
                    events.append(event)
                    kind = event.get("type")
                    print(f"  event: {kind} seq={event.get('seq')}")
                    if kind in {"turn.completed", "turn.failed"}:
                        return events
    return events


async def run(base_url: str, token: str, message: str) -> int:
    external_key = f"e2e:mock:{uuid.uuid4().hex[:8]}"
    idem = f"e2e-{uuid.uuid4().hex}"
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    ) as client:
        print("[1] 鉴权负例：无 token 应 401")
        anon = await httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30.0).post(
            "/api/v1/sessions", json={"external_key": "x"}
        )
        if anon.status_code != 401:
            _fail(f"expected 401 without token, got {anon.status_code}")
        print("  ok")

        print("[2] 幂等建会话")
        first = await client.post("/api/v1/sessions", json={"external_key": external_key, "title": "E2E 桥"})
        first.raise_for_status()
        second = await client.post("/api/v1/sessions", json={"external_key": external_key})
        second.raise_for_status()
        session_id = first.json()["session_id"]
        if second.json()["session_id"] != session_id or second.json()["created"] is not False:
            _fail("external_key get-or-create not idempotent")
        print(f"  ok session_id={session_id}")

        print("[3] 提交消息并消费 SSE 直到终态")
        submit = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"text": message, "sender": {"id": "e2e", "name": "mock-bridge"}},
            headers={"Idempotency-Key": idem},
        )
        submit.raise_for_status()
        body = submit.json()
        if body.get("status") != "started" or not body.get("turn_id"):
            _fail(f"expected started+turn_id, got {body}")
        events = await _consume_until_terminal(client, session_id)
        types = [e["type"] for e in events]
        terminal = [t for t in types if t in {"turn.completed", "turn.failed"}]
        if len(terminal) != 1:
            _fail(f"exactly-one-terminal violated: {types}")
        if "turn.started" not in types:
            _fail(f"missing turn.started: {types}")
        print(f"  ok 终态={terminal[0]}，事件序列={types}")

        print("[4] Idempotency-Key 重复提交应返回 duplicate")
        dup = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"text": message},
            headers={"Idempotency-Key": idem},
        )
        dup.raise_for_status()
        if dup.json().get("status") != "duplicate":
            _fail(f"expected duplicate, got {dup.json()}")
        print("  ok")

        print("[5] Last-Event-ID 回放")
        replay = await _consume_until_terminal(
            client, session_id, last_event_id=1, timeout=15.0
        )
        if not replay or int(replay[0].get("seq") or 0) <= 1:
            _fail("replay did not honor Last-Event-ID")
        print(f"  ok 回放 {len(replay)} 条（seq>{1}）")

        print("[6] 状态快照")
        state = await client.get(f"/api/v1/sessions/{session_id}/state")
        state.raise_for_status()
        print(f"  ok running={state.json()['running']} queued={state.json()['queued_follow_ups']}")

        print("[7] clear 语义：条目保留、上下文清空")
        cleared = await client.delete(f"/api/v1/sessions/{session_id}")
        cleared.raise_for_status()
        if cleared.json().get("cleared") is not True:
            _fail("delete did not return cleared=true")
        listed = await client.get("/api/v1/sessions", params={"external_key": external_key})
        if len(listed.json()["items"]) != 1:
            _fail("registry entry lost after clear")
        print("  ok")

    print("[PASS] external agent api E2E 全链路通过")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18790")
    parser.add_argument("--token", required=True)
    parser.add_argument("--message", default="请只回复「E2E 测试成功」六个字，不要调用任何工具。")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(run(args.base_url, args.token, args.message)))
    except SystemExit:
        raise
    except Exception as exc:
        _fail(f"unexpected error: {exc!r}")


if __name__ == "__main__":
    main()
