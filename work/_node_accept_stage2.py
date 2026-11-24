"""Live acceptance stage 2: a non-continuous task must not receive the memory block."""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18790"
MARKER = "NODE-MEM-NEG-20261124"
DB = "file:D:/NEGI/.g3ku/main-runtime/runtime.sqlite3?mode=ro"


async def main() -> None:
    import websockets

    stage1 = json.loads((ROOT / "work" / "_node_accept_stage1.json").read_text(encoding="utf-8"))
    session_id = stage1["session_id"]
    async with httpx.AsyncClient(base_url=BASE, trust_env=False, timeout=90.0) as client:
        text = (
            f"请用 create_async_task 创建一个**普通一次性任务**（continuous 不要传、不要常驻），"
            f"标题写「节点记忆阴性对照 {MARKER}」，目标与提示词写："
            f"「直接调用 submit_final_result 提交一句『阴性对照完成』，不要调用任何记忆工具，不要做别的事。」"
            f"创建完把 task_id 告诉我。"
        )
        async with websockets.connect(
            f"ws://127.0.0.1:18790/api/ws/ceo?session_id={session_id}", max_size=None
        ) as ws:
            await ws.send(json.dumps({
                "type": "client.user_message", "session_id": session_id, "text": text, "uploads": [],
            }))
            deadline = asyncio.get_event_loop().time() + 360
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
                if str(data.get("type") or "") == "ceo.reply.final":
                    print("FINAL", str(data.get("text") or "")[:200])
                    break

    conn = sqlite3.connect(DB, uri=True)
    rows = list(conn.execute(
        "select task_id, node_id, payload_json from task_model_calls order by seq desc limit 40"
    ))
    target = None
    for task_id, node_id, raw in rows:
        if task_id == stage1.get("task_id"):
            continue
        payload = json.loads(raw)
        ref = str(payload.get("actual_request_ref") or "")
        if not ref.startswith("artifact:"):
            continue
        path = ROOT.parent / "NEGI" / ".g3ku" / "main-runtime" / "artifacts" / task_id.replace(":", "_") / f"artifact_{ref.split(':', 1)[1]}.json"
        if not path.exists():
            continue
        artifact = json.loads(path.read_text(encoding="utf-8"))
        messages = artifact.get("request_messages") or []
        if not messages:
            continue
        system = str(messages[0].get("content") or "")
        target = {
            "task_id": task_id,
            "node_id": node_id,
            "system_chars": len(system),
            "pinned_heading": "## Runtime Contract (pinned)" in system,
            "memory_block_present": "261029:" in system,
            "continuous_duty_prompt": "连续（常驻）节点契约" in system,
        }
        break
    print("NEGATIVE_CONTROL", json.dumps(target, ensure_ascii=False))
    (ROOT / "work" / "_node_accept_stage2.json").write_text(
        json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("STAGE2_WRITTEN")


if __name__ == "__main__":
    asyncio.run(main())