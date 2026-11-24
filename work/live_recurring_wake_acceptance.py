"""Live end-to-end acceptance for the recurring node wake tier.

Drives a real frontdoor turn to create a continuous task whose execution node
arms a recurring schedule_wake (every_seconds=60), then observes the LIVE worker
sweep fire + rearm, the paused silent-advance, and cleans the task up.

Usage: .venv/Scripts/python.exe work/live_recurring_wake_acceptance.py
Result: work/recurring-wake-acceptance-result.json
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import websockets

REPO = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18790"
DB = "file:D:/NEGI/.g3ku/main-runtime/runtime.sqlite3?mode=ro"
SID = "web:ceo-recur-accept"
RESULT = REPO / "work" / "recurring-wake-acceptance-result.json"
PROBE_TITLE = "recurring-wake-probe"

client = httpx.Client(base_url=BASE, trust_env=False, timeout=40)


def log(msg: str) -> None:
    print(msg, flush=True)


def rest_get(path: str):
    return client.get(path).json()


def rest_post(path: str):
    return client.post(path).json()


def rest_delete(path: str):
    return client.delete(path).json()


def db_query(sql: str, args=()):
    con = sqlite3.connect(DB, uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return rows


async def send_turn(text: str, timeout: int = 200) -> list[str]:
    url = f"ws://127.0.0.1:18790/api/ws/ceo?session_id={SID}"
    tools: list[str] = []
    done = asyncio.Event()
    async with websockets.connect(url, max_size=20 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "client.user_message",
            "session_id": SID,
            "messages": [{"text": text, "uploads": []}],
        }))
        log("[sent frontdoor turn]")

        async def reader():
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    done.set()
                    return
                try:
                    env = json.loads(raw)
                except Exception:
                    continue
                t = str(env.get("type", ""))
                data = env.get("data", {}) or {}
                if "tool" in t:
                    nm = data.get("tool") or data.get("name") or data.get("tool_name")
                    if nm:
                        tools.append(str(nm))
                if t in ("message_end", "turn_end", "reply.final", "final", "assistant_message", "error"):
                    done.set()
                    return

        rt = asyncio.create_task(reader())
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        rt.cancel()
    return tools


def find_task():
    data = rest_get("/api/tasks?session_id=all&scope=1")
    for item in data.get("items", []):
        if PROBE_TITLE in str(item.get("title", "")):
            return item
    return None


def recurring_timer(task_id: str):
    rows = db_query(
        "SELECT * FROM task_node_timers WHERE task_id = ? AND kind = 'recurring'", (task_id,)
    )
    return rows[0] if rows else None


def wake_notices(task_id: str):
    return db_query(
        "SELECT node_id, kind, status, message, created_at FROM task_node_notifications "
        "WHERE task_id = ? AND kind = 'wake_timer' ORDER BY created_at", (task_id,)
    )


def wait_for(predicate, timeout: float, interval: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


async def main() -> int:
    result: dict = {"probe": "recurring-node-wake", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    instruction = (
        "用 create_async_task 创建一个连续任务（continuous=true），标题设为 recurring-wake-probe。"
        "任务内容交给执行节点：立即调用 schedule_wake，参数 every_seconds=60、note='recurring-probe'，"
        "设一个周期闹钟；然后调用 enter_wait 停靠待命。每次被闹钟叫醒后什么都不做，继续 enter_wait 待命。"
        "不要创建子节点，不要提交最终结果。创建完成后只回一句话告诉我任务已创建。"
    )
    tools = await send_turn(instruction)
    result["frontdoor_tools"] = sorted(set(tools))
    log(f"[frontdoor tools] {result['frontdoor_tools']}")

    task = wait_for(find_task, timeout=60)
    if not task:
        result["fail"] = "task_not_created"
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log("FAIL: task not created")
        return 1
    task_id = task.get("task_id") or task.get("id")
    result["task_id"] = task_id
    log(f"[task] {task_id}")

    timer = wait_for(lambda: recurring_timer(task_id), timeout=90)
    if not timer:
        result["fail"] = "recurring_timer_not_armed"
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log("FAIL: node did not arm a recurring timer")
        return 1
    initial_wake = int(timer["wake_at_ms"])
    result["armed"] = {
        "node_id": timer["node_id"], "schedule_kind": timer["schedule_kind"],
        "every_ms": timer["every_ms"], "initial_wake_at_ms": initial_wake,
    }
    log(f"[armed] node={timer['node_id']} every_ms={timer['every_ms']} wake_at_ms={initial_wake}")

    # Wait for the live sweep to fire once and rearm to a later slot.
    def fired_and_rearmed():
        notices = wake_notices(task_id)
        t = recurring_timer(task_id)
        if notices and t and int(t["wake_at_ms"]) > initial_wake:
            return {"notices": notices, "timer": t}
        return None

    fired = wait_for(fired_and_rearmed, timeout=120)
    if not fired:
        result["fail"] = "no_fire_within_window"
        result["notices_seen"] = wake_notices(task_id)
        result["timer_seen"] = recurring_timer(task_id)
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log("FAIL: no fire + rearm observed")
        return 1
    rearmed_wake = int(fired["timer"]["wake_at_ms"])
    result["fire"] = {
        "notice_count": len(fired["notices"]),
        "notice_message_excerpt": str(fired["notices"][0]["message"])[:300],
        "rearmed_wake_at_ms": rearmed_wake,
        "claim_cleared": fired["timer"]["claimed_by"] == "",
        "schedule_preserved": fired["timer"]["every_ms"] == timer["every_ms"],
    }
    log(f"[fire] notices={len(fired['notices'])} rearmed_wake_at_ms={rearmed_wake} claim_cleared={result['fire']['claim_cleared']}")

    # Pause: the recurring row should advance silently without filing a new notice.
    notices_before = len(wake_notices(task_id))
    wake_before_pause = int(recurring_timer(task_id)["wake_at_ms"])
    pause_resp = rest_post(f"/api/tasks/{task_id}/pause")
    result["pause_ok"] = bool(pause_resp.get("ok"))
    log(f"[pause] ok={result['pause_ok']}")
    time.sleep(70)
    paused_timer = recurring_timer(task_id)
    notices_after = len(wake_notices(task_id))
    result["paused_silent_advance"] = {
        "wake_before": wake_before_pause,
        "wake_after": int(paused_timer["wake_at_ms"]) if paused_timer else None,
        "advanced": bool(paused_timer and int(paused_timer["wake_at_ms"]) > wake_before_pause),
        "new_notices": notices_after - notices_before,
    }
    log(f"[paused] advanced={result['paused_silent_advance']['advanced']} new_notices={result['paused_silent_advance']['new_notices']}")

    # Cleanup: pause already set; delete the task.
    try:
        del_resp = rest_delete(f"/api/tasks/{task_id}")
        result["cleanup_deleted"] = bool(del_resp.get("ok"))
    except Exception as exc:
        result["cleanup_error"] = str(exc)
    log(f"[cleanup] deleted={result.get('cleanup_deleted')}")

    ok = (
        result.get("armed")
        and result.get("fire", {}).get("claim_cleared")
        and result.get("fire", {}).get("schedule_preserved")
        and result["paused_silent_advance"]["advanced"]
        and result["paused_silent_advance"]["new_notices"] == 0
    )
    result["pass"] = bool(ok)
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log("PASS" if ok else "FAIL: see result json")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))