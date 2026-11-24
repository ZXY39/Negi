"""Live acceptance stage 1: ask the CEO to create a continuous root task."""

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18790"
MARKER = "NODE-MEM-ACCEPT-20261124"


async def main() -> None:
    import websockets

    async with httpx.AsyncClient(base_url=BASE, trust_env=False, timeout=90.0) as client:
        catalog = (await client.get("/api/ceo/sessions")).json()
        original_active = catalog.get("active_session_id")
        created = (await client.post("/api/ceo/sessions", json={"title": "节点记忆验收临时会话"})).json()
        session_id = (created.get("item") or {}).get("session_id")
        print("session", session_id, "original_active", original_active)

        text = (
            f"请用 create_async_task 创建一个**连续（常驻）任务**（continuous=true），"
            f"任务标题写「节点记忆验收 {MARKER}」，目标与提示词写："
            f"「第一轮用 memory_update 把这条临时验收标记记进长期记忆：{MARKER}，"
            f"记完调用 enter_wait(waiting_for=['frontdoor']) 停靠等待，不要做别的事。」"
            f"不要验收节点。创建完把 task_id 告诉我，然后简短回复。"
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
            deadline = asyncio.get_event_loop().time() + 420
            final_text = ""
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
                data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
                kind = str(data.get("type") or msg.get("type") or "")
                if kind == "ceo.reply.final":
                    final_text = str(data.get("text") or "")
                    print("FINAL", final_text[:400])
                    break

        tasks = (await client.get("/api/tasks", params={"limit": 10})).json()
        items = tasks.get("items") or []
        target = next((t for t in items if MARKER in str(t.get("title") or "")), None)
        print("task", json.dumps(target, ensure_ascii=False)[:400] if target else None)
        out = {
            "session_id": session_id,
            "original_active": original_active,
            "task_id": (target or {}).get("task_id"),
            "reply": final_text,
        }
        (ROOT / "work" / "_node_accept_stage1.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("STAGE1_WRITTEN")


if __name__ == "__main__":
    asyncio.run(main())