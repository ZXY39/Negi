"""Live acceptance: active-lane merging, hop count, and non-destructive edits."""

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18790"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, trust_env=False, timeout=60.0) as client:
        before = (await client.get("/api/memory/current")).json()
        items = before.get("items") or []
        snapshot = {str(m.get("memory_id")): str(m.get("memory_body") or "") for m in items}
        baseline = (await client.get("/api/memory/processed", params={"limit": 1})).json()
        baseline_total = int(baseline.get("total") or 0)
        print("resident", len(items), "processed baseline", baseline_total)

        targets = items[:3]
        payloads = [
            {"memory_id": str(m.get("memory_id")), "memory_body": str(m.get("memory_body") or "")}
            for m in targets
        ]
        print("targets", [p["memory_id"] for p in payloads])

        responses = await asyncio.gather(
            *[client.post("/api/memory/current/update", json=p) for p in payloads]
        )
        print("enqueue statuses", [r.status_code for r in responses])
        request_ids = []
        for r in responses:
            body = r.json()
            rid = (body.get("item") or body).get("request_id") if isinstance(body, dict) else None
            request_ids.append(rid)
        print("request_ids", request_ids)

        queue = (await client.get("/api/memory/queue", params={"limit": 20})).json()
        qrows = queue.get("items") or []
        print("queue rows", len(qrows))
        for row in qrows:
            print("   ", row.get("request_id"), row.get("op"), row.get("trigger_source"), row.get("status"))

        deadline = asyncio.get_event_loop().time() + 420
        new_rows = []
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(5)
            page = (await client.get("/api/memory/processed", params={"limit": 10})).json()
            total = int(page.get("total") or 0)
            if total > baseline_total:
                new_rows = (page.get("items") or [])[: total - baseline_total]
                break
        print("new batches", len(new_rows))
        for row in new_rows:
            print(
                "   batch", row.get("batch_id"),
                "request_count", row.get("request_count"),
                "triggers", row.get("trigger_sources"),
                "request_ids", row.get("request_ids"),
                "rounds", row.get("usage_counted_requests"),
                "artifacts", len(row.get("request_artifact_paths") or []),
                "changes", len(row.get("changes") or []),
                "noop", bool(row.get("noop_reason")),
            )

        after = (await client.get("/api/memory/current")).json()
        after_map = {str(m.get("memory_id")): str(m.get("memory_body") or "") for m in (after.get("items") or [])}
        changed = [
            p["memory_id"]
            for p in payloads
            if after_map.get(p["memory_id"]) != snapshot.get(p["memory_id"])
        ]
        print("bodies changed", changed)
        print("resident after", len(after_map))

        result = {
            "baseline_total": baseline_total,
            "request_ids": request_ids,
            "new_batches": [
                {
                    "batch_id": r.get("batch_id"),
                    "request_count": r.get("request_count"),
                    "trigger_sources": r.get("trigger_sources"),
                    "request_ids": r.get("request_ids"),
                    "usage_counted_requests": r.get("usage_counted_requests"),
                    "artifact_count": len(r.get("request_artifact_paths") or []),
                    "change_count": len(r.get("changes") or []),
                    "noop": bool(r.get("noop_reason")),
                }
                for r in new_rows
            ],
            "bodies_changed": changed,
        }
        (ROOT / "work" / "_live_acceptance_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("RESULT_WRITTEN")


if __name__ == "__main__":
    asyncio.run(main())