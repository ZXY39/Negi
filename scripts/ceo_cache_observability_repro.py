from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import websockets

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_WORKSPACE_ROOT = REPO_ROOT.parents[1] if (REPO_ROOT.parents[1] / ".g3ku" / "config.json").exists() else REPO_ROOT
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.utils.helpers import safe_filename

INITIAL_PROMPT = (
    r"Analyze how D:\NewProjects\claude-code-haha-main protects against non-read-only agent actions. "
    r"Save the findings as a markdown analysis document."
)
PROGRESS_PROMPT = "What progress have you made?"
CONTINUE_PROMPT = "Continue the analysis."
AUTO_REPLY = (
    "Continue autonomously. Prefer source-code evidence and runtime evidence. "
    "Save the analysis document directly."
)
FOLLOW_UP_PROMPT = (
    "Continue the analysis and add more code-level evidence, boundary conditions, "
    "and implementation details."
)
REMINDER_COMMAND = (
    r'cmd /c "cd /d D:\NewProjects\claude-code-haha-main && ping -n 41 127.0.0.1 >nul '
    r'&& findstr /N /I /C:permission /C:readonly /C:allowlist /C:denylist /C:sandbox /C:exec src\bootstrap\state.ts package.json"'
)
FORCE_REMINDER_PROMPT = (
    "Before anything else in the next step, run exactly one long-running repo scan with exec. "
    "After any required stage setup, the first non-control tool call must be exec with this exact command: "
    f"`{REMINDER_COMMAND}` "
    "Do not rewrite the command. Do not split it into multiple commands. Do not run any other tool first."
)
REMINDER_INITIAL_PROMPT = (
    "Start analyzing how D:\\NewProjects\\claude-code-haha-main protects against non-read-only agent actions. "
    "Gather initial source-code evidence and begin drafting the markdown analysis, but do not try to finish everything in one turn."
)


@dataclass(slots=True)
class ServerProcess:
    process: subprocess.Popen[str]
    log_path: Path
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def websocket_url(self) -> str:
        return f"ws://{self.host}:{self.port}/api/ws/ceo"


def _python_executable() -> str:
    candidate = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def _server_command(*, host: str, port: int) -> list[str]:
    python_exe = _python_executable()
    code = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
        "from g3ku.web.main import run_server; "
        f"run_server(host={host!r}, port={int(port)}, reload=False, log_level='info')"
    )
    return [python_exe, "-c", code]


async def _wait_for_http_ready(base_url: str, *, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + float(timeout_seconds)
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{base_url}/api/bootstrap/status")
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("ok") is True:
                    return payload
            except Exception:
                await asyncio.sleep(0.5)
                continue
            await asyncio.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for {base_url}/api/bootstrap/status")


async def _unlock_if_needed(base_url: str, *, password: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        status = (await client.get(f"{base_url}/api/bootstrap/status")).json()
        item = dict(status.get("item") or {})
        mode = str(item.get("mode") or "").strip().lower()
        if mode == "unlocked":
            return status
        unlock_response = await client.post(
            f"{base_url}/api/bootstrap/unlock",
            json={"password": password},
        )
        if unlock_response.status_code >= 400:
            setup_response = await client.post(
                f"{base_url}/api/bootstrap/setup",
                json={"password": password, "password_confirm": password},
            )
            setup_response.raise_for_status()
            return setup_response.json()
        return unlock_response.json()


async def _create_session(base_url: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{base_url}/api/ceo/sessions", json={})
        response.raise_for_status()
        payload = response.json()
    item = dict(payload.get("item") or {})
    session_id = str(item.get("session_id") or payload.get("active_session_id") or "").strip()
    if not session_id:
        raise RuntimeError("ceo session creation returned no session_id")
    return session_id


async def _activate_session(base_url: str, session_id: str) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{base_url}/api/ceo/sessions/{quote(session_id, safe='')}/activate")
        response.raise_for_status()


def _actual_request_dir_for_session(session_id: str) -> Path:
    safe_session = safe_filename(str(session_id or "web_shared").replace(":", "_")) or "web_shared"
    return RUNTIME_WORKSPACE_ROOT / ".g3ku" / "web-ceo-requests" / safe_session


def _artifact_rows(session_id: str) -> list[dict[str, Any]]:
    directory = _actual_request_dir_for_session(session_id)
    if not directory.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        usage = dict(payload.get("usage") or {})
        preflight = dict(payload.get("frontdoor_token_preflight_diagnostics") or {})
        rows.append(
            {
                "path": str(path.resolve()),
                "request_id": str(payload.get("request_id") or "").strip(),
                "created_at": str(payload.get("created_at") or "").strip(),
                "request_kind": str(payload.get("request_kind") or payload.get("type") or "").strip(),
                "request_lane": str(payload.get("request_lane") or "visible_frontdoor").strip(),
                "parent_request_id": str(payload.get("parent_request_id") or "").strip(),
                "turn_id": str(payload.get("turn_id") or "").strip(),
                "prompt_cache_key_hash": str(payload.get("prompt_cache_key_hash") or "").strip(),
                "stable_prefix_hash": str(payload.get("stable_prefix_hash") or "").strip(),
                "dynamic_appendix_hash": str(payload.get("dynamic_appendix_hash") or "").strip(),
                "actual_request_hash": str(payload.get("actual_request_hash") or "").strip(),
                "actual_tool_schema_hash": str(payload.get("actual_tool_schema_hash") or "").strip(),
                "provider_model": str(payload.get("provider_model") or "").strip(),
                "frontdoor_history_shrink_reason": str(payload.get("frontdoor_history_shrink_reason") or "").strip(),
                "preflight_applied": bool(preflight.get("applied")),
                "preflight_final_request_tokens": int(preflight.get("final_request_tokens") or 0),
                "preflight_trigger_tokens": int(preflight.get("trigger_tokens") or 0),
                "preflight_effective_trigger_tokens": int(preflight.get("effective_trigger_tokens") or 0),
                "preflight_max_context_tokens": int(preflight.get("max_context_tokens") or 0),
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "cache_hit_tokens": int(usage.get("cache_hit_tokens", 0) or 0),
                "total_billed_input_tokens": int(usage.get("input_tokens", 0) or 0)
                + int(usage.get("cache_hit_tokens", 0) or 0),
            }
        )
    return rows


def _event_timestamp() -> str:
    return datetime.now().isoformat()


"""
def _looks_like_question(text: str) -> bool:
    compact = str(text or "").strip()
    if not compact:
        return False
    question_mark = compact.endswith("?") or compact.endswith("？")
    hint = any(marker in compact for marker in ("请确认", "需要你", "你希望", "你要我", "是否"))
    return question_mark or hint


"""


def _looks_like_question(text: str) -> bool:
    compact = str(text or "").strip()
    if not compact:
        return False
    question_mark = compact.endswith("?") or compact.endswith("？")
    hint = any(marker in compact for marker in ("请确认", "需要你", "你希望", "你要我", "是否"))
    return question_mark or hint


def _safe_session_slug(session_id: str) -> str:
    raw = str(session_id or "").strip() or datetime.now().strftime("%Y%m%d%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in raw)
    return safe[:96] or datetime.now().strftime("%Y%m%d%H%M%S")


def _classify_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    classified: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for row in rows:
        lane = str(row.get("request_lane") or "").strip()
        total_input = int(row.get("total_billed_input_tokens") or 0)
        reason = "steady_state"
        if previous is None:
            reason = "first_request"
        elif lane == "token_compression":
            reason = "allowed_token_compression"
        elif str(row.get("prompt_cache_key_hash") or "") != str(previous.get("prompt_cache_key_hash") or ""):
            reason = "family_reset"
        elif total_input < int(previous.get("total_billed_input_tokens") or 0):
            reason = "potential_context_shrink"
        elif int(row.get("cache_hit_tokens") or 0) < int(previous.get("cache_hit_tokens") or 0):
            reason = "cache_hit_drop"
        classified.append({**row, "analysis_reason": reason})
        previous = row
    return classified


async def _recv_json(ws) -> dict[str, Any]:
    raw = await ws.recv()
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


async def _drive_session(
    *,
    mode: str,
    server: ServerProcess,
    session_id: str,
    output_root: Path,
    max_request_count: int,
    max_runtime_seconds: int,
    max_total_input_tokens: int,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    events_path = output_root / "events.jsonl"
    events_path.write_text("", encoding="utf-8")

    prompt_history: list[dict[str, str]] = []
    collected_events: list[dict[str, Any]] = []
    assistant_finals: list[str] = []

    saw_compression = False
    pause_sent = False
    progress_sent = False
    progress_replied = False
    continue_sent_count = 0
    awaiting_reply: str = ""
    idle_top_up_count = 0
    stop_requested = False
    session_running = False
    current_status = "unknown"
    stop_deadline = 0.0
    abort_reason = ""
    last_activity_at = time.monotonic()
    last_request_count = 0
    current_request_count = 0
    phase_request_baseline = 0
    followup_pause_sent = False
    reminder_seen = False
    reminder_force_sent = False

    async def _record_event(payload: dict[str, Any]) -> None:
        collected_events.append(dict(payload))
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"captured_at": _event_timestamp(), "payload": payload}, ensure_ascii=False) + "\n")

    async def _send_user(ws, text: str, *, label: str) -> None:
        nonlocal awaiting_reply, phase_request_baseline, followup_pause_sent
        prompt_history.append({"label": label, "text": text, "sent_at": _event_timestamp()})
        awaiting_reply = label
        if label == "progress" or label.startswith("continue"):
            phase_request_baseline = current_request_count
            followup_pause_sent = False
        await ws.send(json.dumps({"type": "client.user_message", "text": text}, ensure_ascii=False))

    async def _send_pause(ws, *, final_stop: bool) -> None:
        nonlocal stop_requested, stop_deadline
        if final_stop:
            stop_requested = True
            stop_deadline = time.monotonic() + 20.0
        await ws.send(json.dumps({"type": "client.pause_turn"}, ensure_ascii=False))

    ws_url = f"{server.websocket_url}?session_id={quote(session_id, safe='')}"
    started = time.monotonic()
    async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        while True:
            payload = await _recv_json(ws)
            await _record_event(payload)
            if payload.get("type") == "ceo.sessions.snapshot":
                break
        await _send_user(
            ws,
            REMINDER_INITIAL_PROMPT if mode == "reminder" else INITIAL_PROMPT,
            label="initial",
        )

        while True:
            try:
                payload = await asyncio.wait_for(_recv_json(ws), timeout=1.0)
                await _record_event(payload)
            except asyncio.TimeoutError:
                payload = {}

            event_type = str(payload.get("type") or "").strip()
            data = dict(payload.get("data") or {}) if isinstance(payload.get("data"), dict) else {}
            if event_type:
                last_activity_at = time.monotonic()
            if event_type == "ceo.state":
                state = dict(data.get("state") or {})
                session_running = bool(state.get("is_running")) or str(state.get("status") or "").strip().lower() == "running"
                current_status = str(state.get("status") or "").strip().lower() or current_status
            elif event_type == "ceo.reply.final":
                text = str(data.get("text") or "").strip()
                assistant_finals.append(text)
                session_running = False
                current_status = "completed"
                if awaiting_reply == "progress":
                    progress_replied = True
                    awaiting_reply = ""
                elif awaiting_reply.startswith("continue"):
                    awaiting_reply = ""
                elif _looks_like_question(text):
                    awaiting_reply = ""
                else:
                    awaiting_reply = ""
            elif event_type == "ceo.error":
                text = str(data.get("message") or "").strip()
                assistant_finals.append(text)
                session_running = False
                current_status = "error"
            elif event_type == "ceo.turn.patch":
                inflight_turn = dict(data.get("inflight_turn") or {})
                if inflight_turn:
                    session_running = True
                    current_status = str(inflight_turn.get("status") or "running").strip().lower()
            elif event_type == "ceo.control_ack":
                if str(data.get("action") or "").strip() == "pause":
                    session_running = False
            elif event_type == "ceo.tool.reminder":
                reminder_seen = True

            rows = _artifact_rows(session_id)
            saw_compression = saw_compression or any(str(row.get("request_lane") or "") == "token_compression" for row in rows)
            request_count = len(rows)
            current_request_count = request_count
            if request_count != last_request_count:
                last_request_count = request_count
                last_activity_at = time.monotonic()
            max_total_seen = max((int(row.get("total_billed_input_tokens") or 0) for row in rows), default=0)
            reminder_rows = [
                row
                for row in rows
                if str(row.get("request_lane") or "").strip() == "inline_tool_reminder"
            ]

            if not abort_reason and max_total_seen > int(max_total_input_tokens):
                abort_reason = "input_token_limit_exceeded"
                if not stop_requested:
                    await _send_pause(ws, final_stop=True)
                continue

            if not abort_reason and time.monotonic() - started > float(max_runtime_seconds):
                abort_reason = "timeout"
                if not stop_requested:
                    await _send_pause(ws, final_stop=True)
                continue

            if mode == "reminder":
                if reminder_rows:
                    reminder_seen = True
                    reminder_cache_hit = any(int(row.get("cache_hit_tokens") or 0) > 0 for row in reminder_rows)
                    if reminder_cache_hit:
                        if stop_requested and (not session_running or time.monotonic() >= stop_deadline):
                            break
                        if not stop_requested:
                            await _send_pause(ws, final_stop=True)
                        continue
                    abort_reason = "inline_reminder_cache_miss"
                    if stop_requested and (not session_running or time.monotonic() >= stop_deadline):
                        break
                    if not stop_requested:
                        await _send_pause(ws, final_stop=True)
                    continue
                if stop_requested and (not session_running or time.monotonic() >= stop_deadline):
                    break
                warmup_ready = request_count >= 3 or (request_count >= 2 and not session_running)
                if warmup_ready and not pause_sent and session_running:
                    await _send_pause(ws, final_stop=False)
                    pause_sent = True
                    continue
                if warmup_ready and not reminder_force_sent and not session_running and time.monotonic() - last_activity_at >= 2.5:
                    reminder_force_sent = True
                    await _send_user(ws, FORCE_REMINDER_PROMPT, label="force-reminder")
                    continue
                if request_count >= int(max_request_count):
                    abort_reason = "inline_reminder_not_triggered"
                    if not stop_requested:
                        await _send_pause(ws, final_stop=True)
                    continue
                continue

            if not pause_sent and saw_compression:
                await _send_pause(ws, final_stop=False)
                pause_sent = True
                continue

            if pause_sent and not progress_sent and not session_running and time.monotonic() - last_activity_at >= 2.5:
                await _send_user(ws, PROGRESS_PROMPT, label="progress")
                progress_sent = True
                continue

            if (
                progress_sent
                and not progress_replied
                and awaiting_reply == "progress"
                and session_running
                and not followup_pause_sent
                and request_count >= phase_request_baseline + 4
            ):
                await _send_pause(ws, final_stop=False)
                followup_pause_sent = True
                continue

            if (
                progress_sent
                and not progress_replied
                and awaiting_reply == "progress"
                and followup_pause_sent
                and not session_running
                and time.monotonic() - last_activity_at >= 2.5
            ):
                progress_replied = True
                awaiting_reply = ""
                continue

            if progress_replied and continue_sent_count < 2 and not session_running and not awaiting_reply:
                continue_sent_count += 1
                await _send_user(ws, CONTINUE_PROMPT, label=f"continue-{continue_sent_count}")
                continue

            if (
                awaiting_reply.startswith("continue")
                and session_running
                and not followup_pause_sent
                and request_count >= phase_request_baseline + 4
            ):
                await _send_pause(ws, final_stop=False)
                followup_pause_sent = True
                continue

            if (
                awaiting_reply.startswith("continue")
                and followup_pause_sent
                and not session_running
                and time.monotonic() - last_activity_at >= 2.5
            ):
                awaiting_reply = ""
                continue

            if request_count >= int(max_request_count) and not stop_requested:
                await _send_pause(ws, final_stop=True)
                continue

            if stop_requested and (not session_running or time.monotonic() >= stop_deadline):
                break

            if (
                continue_sent_count >= 2
                and not reminder_seen
                and not reminder_force_sent
                and not session_running
                and not awaiting_reply
                and request_count < int(max_request_count)
            ):
                reminder_force_sent = True
                await _send_user(ws, FORCE_REMINDER_PROMPT, label="force-reminder")
                continue

            if continue_sent_count >= 2 and not session_running and not awaiting_reply and request_count < int(max_request_count):
                last_text = assistant_finals[-1] if assistant_finals else ""
                if _looks_like_question(last_text):
                    await _send_user(ws, AUTO_REPLY, label=f"auto-answer-{idle_top_up_count + 1}")
                else:
                    idle_top_up_count += 1
                    await _send_user(ws, FOLLOW_UP_PROMPT, label=f"follow-up-{idle_top_up_count}")
                continue

    rows = _classify_rows(_artifact_rows(session_id))
    report = {
        "session_id": session_id,
        "captured_at": _event_timestamp(),
        "prompt_history": prompt_history,
        "assistant_finals": assistant_finals,
        "event_count": len(collected_events),
        "artifact_count": len(rows),
        "compression_count": sum(1 for row in rows if row["request_lane"] == "token_compression"),
        "visible_request_count": sum(1 for row in rows if row["request_lane"] == "visible_frontdoor"),
        "internal_request_count": sum(1 for row in rows if row["request_lane"] != "visible_frontdoor"),
        "abort_reason": abort_reason,
        "max_total_billed_input_tokens_seen": max(
            (int(row.get("total_billed_input_tokens") or 0) for row in rows),
            default=0,
        ),
        "rows": rows,
    }
    (output_root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


async def _run_repro(
    *,
    mode: str,
    host: str,
    port: int,
    unlock_password: str,
    output_root: Path,
    max_request_count: int,
    max_runtime_seconds: int,
    max_total_input_tokens: int,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    server_log = output_root / "web-server.log"
    server_log_handle = server_log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        _server_command(host=host, port=port),
        cwd=str(RUNTIME_WORKSPACE_ROOT),
        stdout=server_log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT)
            if not os.environ.get("PYTHONPATH")
            else str(REPO_ROOT) + os.pathsep + str(os.environ.get("PYTHONPATH") or ""),
        },
    )
    server = ServerProcess(process=process, log_path=server_log, host=host, port=port)
    try:
        status = await _wait_for_http_ready(server.base_url, timeout_seconds=60)
        await _unlock_if_needed(server.base_url, password=unlock_password)
        session_id = await _create_session(server.base_url)
        await _activate_session(server.base_url, session_id)
        report = await _drive_session(
            mode=mode,
            server=server,
            session_id=session_id,
            output_root=output_root / _safe_session_slug(session_id),
            max_request_count=(
                4
                if mode == "smoke"
                else min(int(max_request_count), 15)
                if mode == "reminder"
                else max_request_count
            ),
            max_runtime_seconds=(
                90
                if mode == "smoke"
                else min(int(max_runtime_seconds), 300)
                if mode == "reminder"
                else max_runtime_seconds
            ),
            max_total_input_tokens=max_total_input_tokens,
        )
        report["bootstrap_status"] = status
        report["server_log"] = str(server_log.resolve())
        report["mode"] = mode
        report["runtime_workspace_root"] = str(RUNTIME_WORKSPACE_ROOT.resolve())
        if str(report.get("abort_reason") or "").strip() == "input_token_limit_exceeded":
            raise RuntimeError(
                f"Replay aborted because a request exceeded {max_total_input_tokens} total billed input tokens"
            )
        if mode == "reminder":
            inline_rows = [
                row
                for row in list(report.get("rows") or [])
                if str(row.get("request_lane") or "").strip() == "inline_tool_reminder"
            ]
            if not inline_rows:
                raise RuntimeError("Reminder replay did not trigger inline_tool_reminder")
            if not any(int(row.get("cache_hit_tokens") or 0) > 0 for row in inline_rows):
                raise RuntimeError("Reminder replay triggered inline_tool_reminder but no cache hit was preserved")
        if mode == "full" and int(report.get("compression_count") or 0) <= 0:
            raise RuntimeError("Full replay did not trigger token_compression")
        return report
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
        server_log_handle.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a real Web CEO cache observability replay.")
    parser.add_argument("--mode", choices=["smoke", "full", "reminder"], default="full")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18796)
    parser.add_argument("--unlock-password", default="qaz")
    parser.add_argument("--max-request-count", type=int, default=30)
    parser.add_argument("--max-runtime-seconds", type=int, default=1800)
    parser.add_argument("--max-total-input-tokens", type=int, default=40000)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / ".g3ku" / "ceo-cache-repro" / datetime.now().strftime("%Y%m%dT%H%M%S"),
    )
    return parser


async def _async_main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    report = await _run_repro(
        mode=str(args.mode),
        host=str(args.host),
        port=int(args.port),
        unlock_password=str(args.unlock_password),
        output_root=Path(args.output_root),
        max_request_count=int(args.max_request_count),
        max_runtime_seconds=int(args.max_runtime_seconds),
        max_total_input_tokens=int(args.max_total_input_tokens),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
