"""Live acceptance: real conversation turn, review singleton, mutating-batch hop count."""

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18790"
MARKER = "ML-ACCEPT-20261124"


async def processed_total(client) -> int:
    page = (await client.get("/api/memory/processed", params={"limit": 1})).json()
    return int(page.get("total") or 0)


async def wait_new_batches(client, baseline: int, timeout: float = 420.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(5)
        page = (await client.get("/api/memory/processed", params={"limit": 12})).json()
        total = int(page.get("total") or 0)
        if total > baseline:
            return (page.get("items") or [])[: total - baseline]
    return []


def describe(row) -> dict:
    return {
        "batch_id": row.get("batch_id"),
        "op": row.get("op"),
        "request_count": row.get("request_count"),
        "trigger_sources": row.get("trigger_sources"),
        "rounds": row.get("usage_counted_requests"),
        "artifacts": len(row.get("request_artifact_paths") or []),
        "changes": len(row.get("changes") or []),
        "noop": bool(row.get("noop_reason")),
    }


async def main() -> None:
    import websockets

    out = {}
    async with httpx.AsyncClient(base_url=BASE, trust_env=False, timeout=90.0) as client:
        catalog = (await client.get("/api/ceo/sessions")).json()
        original_active = catalog.get("active_session_id")
        created = (await client.post("/api/ceo/sessions", json={"title": "记忆车道验收临时会话"})).json()
        session_id = (created.get("item") or {}).get("session_id")
        print("throwaway session", session_id, "original active", original_active)
        out["session_id"] = session_id

        baseline = await processed_total(client)
        text = (
            f"请记住这条临时验收条目：{MARKER}，它只是记忆车道实机验收用的标记，"
            "验收结束后会被删除。请用 memory_update 把它记下来，然后简短回复已记住。"
        )
        async with websockets.connect(
            f"ws://127.0.0.1:18790/api/ws/ceo?session_id={session_id}", max_size=None
        ) as ws:
            await ws.send(json.dumps({
                "type": "client.user_message",
                "session_id": session_id,
                "text": text,
                "uploads": [],
            }))
            seen = []
            deadline = asyncio.get_event_loop().time() + 300
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                except Exception as exc:
                    print("ws closed", exc)
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                kind = str(msg.get("type") or "")
                event = msg.get("event") or msg.get("item") or {}
                inner = str(event.get("type") or "") if isinstance(event, dict) else ""
                label = inner or kind
                if label not in seen:
                    seen.append(label)
                if label == "message_end":
                    print("turn finished")
                    break
            print("event kinds", seen[:14])

        rows = await wait_new_batches(client, baseline)
        out["after_turn"] = [describe(r) for r in rows]
        print("after turn batches", json.dumps(out["after_turn"], ensure_ascii=False))
        baseline2 = await processed_total(client)

        current = (await client.get("/api/memory/current")).json()
        target = next(
            (m for m in (current.get("items") or []) if MARKER in str(m.get("memory_body") or "")),
            None,
        )
        out["marker_memory_id"] = str(target.get("memory_id")) if target else None
        print("marker memory", out["marker_memory_id"])

        deleted = await client.delete(f"/api/ceo/sessions/{session_id}")
        print("session delete", deleted.status_code)
        rows2 = await wait_new_batches(client, baseline2)
        out["after_session_delete"] = [describe(r) for r in rows2]
        print("after session delete", json.dumps(out["after_session_delete"], ensure_ascii=False))
        baseline3 = await processed_total(client)

        if target:
            resp = await client.post(
                "/api/memory/current/delete",
                json={"memory_ids": [str(target.get("memory_id"))], "reason": "实机验收临时条目清理"},
            )
            print("memory delete enqueue", resp.status_code)
            rows3 = await wait_new_batches(client, baseline3)
            out["after_memory_delete"] = [describe(r) for r in rows3]
            print("after memory delete", json.dumps(out["after_memory_delete"], ensure_ascii=False))

        if original_active:
            await client.post(f"/api/ceo/sessions/{original_active}/activate")
        final = (await client.get("/api/memory/current")).json()
        out["marker_still_present"] = any(
            MARKER in str(m.get("memory_body") or "") for m in (final.get("items") or [])
        )
        out["resident_final"] = len(final.get("items") or [])
        print("marker still present", out["marker_still_present"], "resident", out["resident_final"])

    (ROOT / "work" / "_live_acceptance_turn.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("RESULT_WRITTEN")


if __name__ == "__main__":
    asyncio.run(main())