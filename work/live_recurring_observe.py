"""Observation-only live acceptance for the already-armed recurring probe node.

The directed probe armed a recurring timer (every_ms=60000) on node:76789423f856
but crashed on a wrong column name before observing. This reads the live state,
waits for the next sweep fire + rearm, checks the paused silent-advance, cleans up.

Usage: .venv/Scripts/python.exe work/live_recurring_observe.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18790"
DB = "file:D:/NEGI/.g3ku/main-runtime/runtime.sqlite3?mode=ro"
RESULT = REPO / "work" / "recurring-wake-observe-result.json"
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
    rows = db_query(
        "SELECT kind, schedule_kind, every_ms, cron_expr, wake_at_ms, claimed_by, claimed_at_ms "
        "FROM task_node_timers WHERE task_id=? AND node_id=? AND kind='recurring'", (TASK, NODE),
    )
    return rows[0] if rows else None


def wake_notices():
    rows = db_query(
        "SELECT status, created_at, payload_json FROM task_node_notifications "
        "WHERE task_id=? AND node_id=? ORDER BY created_at", (TASK, NODE),
    )
    out = []
    for r in rows:
        try:
            p = json.loads(r["payload_json"] or "{}")
        except Exception:
            p = {}
        if str(p.get("kind", "")) == "wake_timer":
            out.append({"status": r["status"], "created_at": r["created_at"], "message": str(p.get("message", ""))})
    return out


def wait_for(pred, timeout, interval=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return None


def main():
    result = {"task_id": TASK, "node_id": NODE, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    timer = recurring_timer()
    if not timer:
        result["fail"] = "no_recurring_timer"
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log("FAIL: no recurring timer present")
        return 1
    baseline_wake = int(timer["wake_at_ms"])
    baseline_notices = len(wake_notices())
    result["armed"] = {
        "schedule_kind": timer["schedule_kind"], "every_ms": timer["every_ms"],
        "baseline_wake_at_ms": baseline_wake, "baseline_notices": baseline_notices,
    }
    log(f"[armed] every_ms={timer['every_ms']} baseline_wake={baseline_wake} baseline_notices={baseline_notices}")

    def fired():
        n = wake_notices(); t = recurring_timer()
        if t and len(n) > baseline_notices and int(t["wake_at_ms"]) > baseline_wake:
            return {"n": n, "t": t}
        return None

    f = wait_for(fired, timeout=120)
    if not f:
        result["fail"] = "no_fire"
        result["notices"] = len(wake_notices()); result["timer"] = recurring_timer()
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log("FAIL: no fire+rearm observed")
        return 1
    result["fire"] = {
        "new_notices": len(f["n"]) - baseline_notices,
        "excerpt": f["n"][-1]["message"][:300],
        "rearmed_wake_at_ms": int(f["t"]["wake_at_ms"]),
        "claim_cleared": f["t"]["claimed_by"] == "",
        "schedule_preserved": f["t"]["every_ms"] == timer["every_ms"],
    }
    log(f"[fire] new_notices={result['fire']['new_notices']} rearmed={result['fire']['rearmed_wake_at_ms']} claim_cleared={result['fire']['claim_cleared']} preserved={result['fire']['schedule_preserved']}")

    nb = len(wake_notices()); wb = int(recurring_timer()["wake_at_ms"])
    pr = client.post(f"/api/tasks/{TASK}/pause").json()
    result["pause_ok"] = bool(pr.get("ok"))
    log(f"[pause] ok={result['pause_ok']}")
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

    ok = (result["fire"]["claim_cleared"] and result["fire"]["schedule_preserved"]
          and result["paused_silent_advance"]["advanced"] and result["paused_silent_advance"]["new_notices"] == 0
          and result.get("cleanup_deleted"))
    result["pass"] = bool(ok)
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())