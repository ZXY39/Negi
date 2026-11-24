"""Directed live acceptance: force the existing probe node to arm a recurring wake.

The first probe node armed a one-shot out of habit; this sends it an explicit
node message demanding every_seconds, then observes the LIVE sweep fire + rearm
and the paused silent-advance, and cleans up.

Usage: .venv/Scripts/python.exe work/live_recurring_wake_directed.py
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
RESULT = REPO / "work" / "recurring-wake-direct-result.json"
TASK = "task:6ebf8a5f59ed"
NODE = "node:76789423f856"

client = httpx.Client(base_url=BASE, trust_env=False, timeout=40)


def log(m):
    print(m, flush=True)


def db_query(sql, args=()):
    con = sqlite3.connect(DB, uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return rows


def recurring_timer():
    rows = db_query("SELECT * FROM task_node_timers WHERE task_id=? AND node_id=? AND kind='recurring'", (TASK, NODE))
    return rows[0] if rows else None


def wake_notices():
    return db_query("SELECT status, message, created_at FROM task_node_notifications WHERE task_id=? AND node_id=? AND kind='wake_timer' ORDER BY created_at", (TASK, NODE))


async def send_turn(text, timeout=200):
    url = f"ws://127.0.0.1:18790/api/ws/ceo?session_id={SID}"
    done = asyncio.Event()
    tools = []
    async with websockets.connect(url, max_size=20 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "client.user_message", "session_id": SID, "messages": [{"text": text, "uploads": []}]}))
        log("[sent]")

        async def reader():
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    done.set(); return
                try:
                    env = json.loads(raw)
                except Exception:
                    continue
                t = str(env.get("type", "")); data = env.get("data", {}) or {}
                if "tool" in t:
                    nm = data.get("tool") or data.get("name") or data.get("tool_name")
                    if nm: tools.append(str(nm))
                if t in ("message_end", "turn_end", "reply.final", "final", "assistant_message", "error"):
                    done.set(); return
        rt = asyncio.create_task(reader())
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        rt.cancel()
    return tools


def wait_for(pred, timeout, interval=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return None


async def main():
    result = {"task_id": TASK, "node_id": NODE, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    msg = (
        f"用 send_message 给节点 {NODE}（任务 {TASK}）发一条消息，正文原样如下："
        "「立即调用 schedule_wake，必须传 every_seconds=60 和 note='recurring-probe'。"
        "every_seconds 是周期档参数，会自动重臂；禁止使用 after_seconds，禁止使用 at，禁止使用 cron_expr。"
        "设好周期闹钟后调用 enter_wait 待命，被叫醒后什么也不做继续 enter_wait。」"
        "发完后只回一句话。"
    )
    tools = await send_turn(msg)
    result["frontdoor_tools"] = sorted(set(tools))
    log(f"[frontdoor tools] {result['frontdoor_tools']}")

    timer = wait_for(recurring_timer, timeout=120)
    if not timer:
        result["fail"] = "recurring_not_armed"
        result["timers_all"] = db_query("SELECT kind,schedule_kind,every_ms,wake_at_ms FROM task_node_timers WHERE task_id=? AND node_id=?", (TASK, NODE))
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log("FAIL: node still did not arm recurring")
        return 1
    initial = int(timer["wake_at_ms"])
    result["armed"] = {"schedule_kind": timer["schedule_kind"], "every_ms": timer["every_ms"], "initial_wake_at_ms": initial}
    log(f"[armed recurring] every_ms={timer['every_ms']} wake_at_ms={initial}")

    def fired():
        n = wake_notices(); t = recurring_timer()
        if n and t and int(t["wake_at_ms"]) > initial:
            return {"n": n, "t": t}
        return None

    f = wait_for(fired, timeout=120)
    if not f:
        result["fail"] = "no_fire"
        result["notices"] = wake_notices(); result["timer"] = recurring_timer()
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log("FAIL: no fire+rearm")
        return 1
    result["fire"] = {
        "notice_count": len(f["n"]),
        "excerpt": str(f["n"][0]["message"])[:300],
        "rearmed_wake_at_ms": int(f["t"]["wake_at_ms"]),
        "claim_cleared": f["t"]["claimed_by"] == "",
        "schedule_preserved": f["t"]["every_ms"] == timer["every_ms"],
    }
    log(f"[fire] notices={len(f['n'])} rearmed={result['fire']['rearmed_wake_at_ms']} claim_cleared={result['fire']['claim_cleared']}")

    nb = len(wake_notices()); wb = int(recurring_timer()["wake_at_ms"])
    pr = client.post(f"/api/tasks/{TASK}/pause").json()
    result["pause_ok"] = bool(pr.get("ok"))
    time.sleep(70)
    pt = recurring_timer(); na = len(wake_notices())
    result["paused_silent_advance"] = {
        "advanced": bool(pt and int(pt["wake_at_ms"]) > wb),
        "new_notices": na - nb,
        "wake_before": wb, "wake_after": int(pt["wake_at_ms"]) if pt else None,
    }
    log(f"[paused] advanced={result['paused_silent_advance']['advanced']} new_notices={result['paused_silent_advance']['new_notices']}")

    try:
        dr = client.delete(f"/api/tasks/{TASK}").json()
        result["cleanup_deleted"] = bool(dr.get("ok"))
    except Exception as exc:
        result["cleanup_error"] = str(exc)
    log(f"[cleanup] deleted={result.get('cleanup_deleted')}")

    ok = (result.get("armed") and result["fire"]["claim_cleared"] and result["fire"]["schedule_preserved"]
          and result["paused_silent_advance"]["advanced"] and result["paused_silent_advance"]["new_notices"] == 0)
    result["pass"] = bool(ok)
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))