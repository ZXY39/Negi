from __future__ import annotations

import argparse
import asyncio
import json
import os
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
WORKSPACE_ROOT = REPO_ROOT.parents[1] if (REPO_ROOT.parents[1] / ".g3ku" / "config.json").exists() else REPO_ROOT
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.bus.queue import MessageBus
from g3ku.config.loader import load_config
from g3ku.runtime.bootstrap_factory import make_agent_loop, make_provider
from g3ku.security.bootstrap import get_bootstrap_security_service
from g3ku.utils.helpers import safe_filename


CEO_INITIAL_PROMPT = (
    r"深入分析D:\NewProjects\claude-code-haha-main项目是怎么实现对agent的非只读操作进行防护的，形成分析文档保存下来"
)
CEO_PROGRESS_PROMPT = "进度到哪了"
CEO_CONTINUE_PROMPT = "继续分析"
CEO_AUTO_REPLY = (
    "选项A：保存到 docs/agent-non-readonly-guardrails.md。继续自主完成，不要停下来等我。"
    "优先基于源代码与运行时证据继续推进，并把分析文档直接保存下来。"
)
CEO_FOLLOW_UP_PROMPT = (
    "继续分析，并增加更多代码级证据、关键边界、保护链路、失败场景与风险点。"
)
NODE_TASK_PROMPT = (
    r"深入分析 D:\NewProjects\claude-code-haha-main 项目如何防护 agent 的非只读操作。"
    r"请持续收集源代码证据，形成 markdown 分析文档并保存。"
    r"报告至少覆盖：架构边界、命令执行防护、文件写入/删除防护、审批/中断机制、错误恢复、缓存或上下文相关风险。"
)
NODE_APPEND_NOTICE_PROMPT = (
    "继续分析，并补充更多代码级证据、边界条件、失败场景、绕过面与运行时保护链路。"
)
NODE_SESSION_ID = f"web:contract-acceptance-node:{datetime.now().strftime('%Y%m%d%H%M%S')}"
CEO_WS_EVENT_TIMEOUT_SECONDS = 1.0
DEFAULT_MAX_ROUNDS = 30
DEFAULT_MAX_RUNTIME_SECONDS = 1800
TOKEN_COMPACT_MARKER = "[G3KU_TOKEN_COMPACT_V2]"


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


async def _wait_for_http_ready(base_url: str, *, timeout_seconds: int = 60) -> dict[str, Any]:
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
        unlock_response.raise_for_status()
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


def _ensure_local_bootstrap_unlocked(*, password: str) -> None:
    service = get_bootstrap_security_service(WORKSPACE_ROOT)
    status = dict(service.status() or {})
    if str(status.get("mode") or "").strip().lower() == "unlocked":
        return
    try:
        service.unlock(password=password)
        return
    except Exception:
        pass
    service.setup_initial_realm(password=password, confirm_legacy_reset=False)


async def _recv_json(ws) -> dict[str, Any]:
    raw = await ws.recv()
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def _safe_slug(value: str) -> str:
    raw = str(value or "").strip() or datetime.now().strftime("%Y%m%d%H%M%S")
    safe = safe_filename(raw.replace(":", "_")) or datetime.now().strftime("%Y%m%d%H%M%S")
    return safe[:96]


def _ceo_actual_request_dir(session_id: str) -> Path:
    return WORKSPACE_ROOT / ".g3ku" / "web-ceo-requests" / _safe_slug(session_id)


def _ceo_rows(session_id: str) -> list[dict[str, Any]]:
    directory = _ceo_actual_request_dir(session_id)
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
                "created_at": str(payload.get("created_at") or "").strip(),
                "request_id": str(payload.get("request_id") or "").strip(),
                "turn_id": str(payload.get("turn_id") or "").strip(),
                "request_kind": str(payload.get("request_kind") or payload.get("type") or "").strip(),
                "request_lane": str(payload.get("request_lane") or "").strip(),
                "parent_request_id": str(payload.get("parent_request_id") or "").strip(),
                "prompt_cache_key_hash": str(payload.get("prompt_cache_key_hash") or "").strip(),
                "stable_prefix_hash": str(payload.get("stable_prefix_hash") or "").strip(),
                "dynamic_appendix_hash": str(payload.get("dynamic_appendix_hash") or "").strip(),
                "actual_request_hash": str(payload.get("actual_request_hash") or "").strip(),
                "actual_tool_schema_hash": str(payload.get("actual_tool_schema_hash") or "").strip(),
                "frontdoor_history_shrink_reason": str(payload.get("frontdoor_history_shrink_reason") or "").strip(),
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_hit_tokens": int(usage.get("cache_hit_tokens") or 0),
                "total_billed_input_tokens": int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_hit_tokens") or 0),
                "preflight_applied": bool(preflight.get("applied")),
                "preflight_final_request_tokens": int(preflight.get("final_request_tokens") or 0),
                "preflight_trigger_tokens": int(preflight.get("trigger_tokens") or 0),
                "preflight_effective_trigger_tokens": int(preflight.get("effective_trigger_tokens") or 0),
                "preflight_max_context_tokens": int(preflight.get("max_context_tokens") or 0),
            }
        )
    rows.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("request_id") or "")))
    return rows


def _classify_ceo_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        elif str(row.get("frontdoor_history_shrink_reason") or "").strip() in {"token_compression", "stage_compaction"}:
            reason = "allowed_history_shrink"
        elif str(row.get("prompt_cache_key_hash") or "") != str(previous.get("prompt_cache_key_hash") or ""):
            reason = "family_reset"
        elif total_input < int(previous.get("total_billed_input_tokens") or 0):
            reason = "potential_context_shrink"
        elif int(row.get("cache_hit_tokens") or 0) < int(previous.get("cache_hit_tokens") or 0):
            reason = "cache_hit_drop"
        classified.append({**row, "analysis_reason": reason})
        previous = row
    return classified


def _looks_like_question(text: str) -> bool:
    compact = str(text or "").strip()
    if not compact:
        return False
    if compact.endswith("?") or compact.endswith("？"):
        return True
    return any(marker in compact for marker in ("请确认", "需要你", "你希望", "是否", "要不要", "能否"))


def _persist_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


async def _run_ceo_acceptance(
    *,
    server: ServerProcess,
    unlock_password: str,
    output_dir: Path,
    max_rounds: int,
    max_runtime_seconds: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    await _wait_for_http_ready(server.base_url, timeout_seconds=60)
    bootstrap_status = await _unlock_if_needed(server.base_url, password=unlock_password)
    session_id = await _create_session(server.base_url)
    await _activate_session(server.base_url, session_id)

    events_path = output_dir / "events.jsonl"
    if events_path.exists():
        events_path.unlink()
    ws_url = f"{server.websocket_url}?session_id={quote(session_id, safe='')}"

    assistant_finals: list[str] = []
    prompt_history: list[dict[str, Any]] = []
    saw_compression = False
    manual_pause_sent = False
    progress_sent = False
    progress_replied = False
    continue_sent_count = 0
    continue_progress_count = 0
    current_label = ""
    session_running = False
    last_activity_at = time.monotonic()
    start_time = time.monotonic()
    current_request_count = 0
    phase_request_baseline = 0

    async def _record_event(payload: dict[str, Any]) -> None:
        _write_jsonl(
            events_path,
            {"captured_at": datetime.now().isoformat(), "payload": payload},
        )

    async def _send_user(ws, text: str, *, label: str) -> None:
        nonlocal current_label, phase_request_baseline
        current_label = label
        phase_request_baseline = current_request_count
        prompt_history.append({"label": label, "text": text, "sent_at": datetime.now().isoformat()})
        await ws.send(json.dumps({"type": "client.user_message", "text": text}, ensure_ascii=False))

    async def _send_pause(ws) -> None:
        await ws.send(json.dumps({"type": "client.pause_turn"}, ensure_ascii=False))

    async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        while True:
            hello = await _recv_json(ws)
            await _record_event(hello)
            if str(hello.get("type") or "").strip() == "ceo.sessions.snapshot":
                break

        await _send_user(ws, CEO_INITIAL_PROMPT, label="initial")

        while True:
            if time.monotonic() - start_time >= float(max_runtime_seconds):
                break

            try:
                payload = await asyncio.wait_for(_recv_json(ws), timeout=CEO_WS_EVENT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                payload = {}
            if payload:
                await _record_event(payload)
                last_activity_at = time.monotonic()

            event_type = str(payload.get("type") or "").strip()
            data = dict(payload.get("data") or {}) if isinstance(payload.get("data"), dict) else {}
            if event_type == "ceo.state":
                state = dict(data.get("state") or {})
                session_running = bool(state.get("is_running")) or str(state.get("status") or "").strip().lower() == "running"
            elif event_type == "ceo.turn.patch":
                inflight_turn = dict(data.get("inflight_turn") or {})
                if inflight_turn:
                    session_running = True
            elif event_type == "ceo.control_ack" and str(data.get("action") or "").strip().lower() == "pause":
                session_running = False
            elif event_type == "ceo.reply.final":
                text = str(data.get("text") or "").strip()
                assistant_finals.append(text)
                session_running = False
                if current_label == "progress":
                    progress_replied = True
                elif current_label.startswith("continue"):
                    continue_progress_count = max(continue_progress_count, continue_sent_count)
                if _looks_like_question(text) and continue_progress_count < 2:
                    await _send_user(ws, CEO_AUTO_REPLY, label=f"auto-reply-{len(prompt_history) + 1}")
                    continue
                current_label = ""
            elif event_type == "ceo.error":
                assistant_finals.append(str(data.get("message") or "").strip())
                session_running = False

            rows = _classify_ceo_rows(_ceo_rows(session_id))
            visible_round_count = len(rows)
            current_request_count = visible_round_count
            saw_compression = saw_compression or any(
                str(row.get("request_lane") or "").strip() == "token_compression"
                or str(row.get("frontdoor_history_shrink_reason") or "").strip() == "token_compression"
                for row in rows
            )

            if current_label.startswith("auto-reply") and visible_round_count > phase_request_baseline:
                current_label = ""
            if current_label.startswith("continue") and visible_round_count > phase_request_baseline:
                continue_progress_count = max(continue_progress_count, continue_sent_count)
                current_label = ""

            if visible_round_count >= int(max_rounds):
                if session_running:
                    await _send_pause(ws)
                break

            if saw_compression and not manual_pause_sent and session_running:
                await _send_pause(ws)
                manual_pause_sent = True
                continue

            if manual_pause_sent and not progress_sent and not session_running and time.monotonic() - last_activity_at >= 2.0:
                await _send_user(ws, CEO_PROGRESS_PROMPT, label="progress")
                progress_sent = True
                continue

            if progress_replied and continue_sent_count < 2 and not current_label:
                continue_sent_count += 1
                await _send_user(ws, CEO_CONTINUE_PROMPT, label=f"continue-{continue_sent_count}")
                continue

            if (
                not saw_compression
                and not session_running
                and not current_label
                and time.monotonic() - last_activity_at >= 2.0
            ):
                await _send_user(ws, CEO_FOLLOW_UP_PROMPT, label=f"follow-up-{len(prompt_history) + 1}")
                continue

            if (
                saw_compression
                and manual_pause_sent
                and progress_replied
                and continue_progress_count >= 2
                and not session_running
            ):
                break

    rows = _classify_ceo_rows(_ceo_rows(session_id))
    report = {
        "session_id": session_id,
        "bootstrap_status": bootstrap_status,
        "prompt_history": prompt_history,
        "assistant_finals": assistant_finals,
        "event_log_path": str(events_path.resolve()),
        "artifact_dir": str(_ceo_actual_request_dir(session_id).resolve()),
        "row_count": len(rows),
        "conditions": {
            "compression_triggered": any(
                str(row.get("request_lane") or "").strip() == "token_compression"
                or str(row.get("frontdoor_history_shrink_reason") or "").strip() == "token_compression"
                for row in rows
            ),
            "manual_pause_sent": manual_pause_sent,
            "progress_reply_observed": progress_replied,
            "continue_follow_up_count": continue_progress_count,
        },
        "rows": rows,
        "stop_reason": (
            "success"
            if any(str(row.get("request_lane") or "").strip() == "token_compression" for row in rows)
            and manual_pause_sent
            and progress_replied
            and continue_progress_count >= 2
            else "round_limit" if len(rows) >= int(max_rounds) else "timeout_or_early_stop"
        ),
    }
    _persist_json(output_dir / "report.json", report)
    return report


def _primary_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage_by_model = list(payload.get("delta_usage_by_model") or [])
    if not usage_by_model:
        return {}
    first = usage_by_model[0]
    return dict(first or {}) if isinstance(first, dict) else {}


def _configure_isolated_main_runtime(config, *, session_id: str) -> Path:
    runtime_root = REPO_ROOT / ".g3ku" / "contract-cache-acceptance-runtime" / _safe_slug(session_id)
    runtime_root.mkdir(parents=True, exist_ok=True)
    tasks_dir = runtime_root / "tasks"
    artifacts_dir = runtime_root / "artifacts"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    config.main_runtime.store_path = runtime_root / "runtime.sqlite3"
    config.main_runtime.files_base_dir = tasks_dir
    config.main_runtime.artifact_dir = artifacts_dir
    config.main_runtime.governance_store_path = runtime_root / "governance.sqlite3"
    return runtime_root


def _resolve_content_ref(service, ref: str) -> dict[str, Any]:
    raw_ref = str(ref or "").strip()
    if not raw_ref:
        return {}
    try:
        resolved = service.log_service.resolve_content_ref(raw_ref)
    except Exception:
        return {}
    try:
        payload = json.loads(str(resolved or ""))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _node_rows(service, task_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in reversed(list(service.store.list_task_model_calls(task_id, limit=500) or [])):
        payload = dict(item.get("payload") or {})
        usage = _primary_usage(payload)
        actual_request_ref = str(payload.get("actual_request_ref") or "").strip()
        actual_request_payload = _resolve_content_ref(service, actual_request_ref)
        request_messages = [
            dict(message)
            for message in list(actual_request_payload.get("request_messages") or [])
            if isinstance(message, dict)
        ]
        request_message_count = int(actual_request_payload.get("prepared_message_count") or len(request_messages))
        rows.append(
            {
                "seq": int(item.get("seq") or 0),
                "created_at": str(item.get("created_at") or "").strip(),
                "node_id": str(item.get("node_id") or "").strip(),
                "call_index": int(payload.get("call_index") or 0),
                "prompt_cache_key_hash": str(payload.get("prompt_cache_key_hash") or "").strip(),
                "actual_request_hash": str(payload.get("actual_request_hash") or "").strip(),
                "actual_tool_schema_hash": str(payload.get("actual_tool_schema_hash") or "").strip(),
                "actual_request_ref": actual_request_ref,
                "request_message_count": request_message_count,
                "callable_tool_names": list(payload.get("callable_tool_names") or []),
                "provider_tool_names": list(payload.get("provider_tool_names") or []),
                "provider_tool_bundle_seeded": bool(payload.get("provider_tool_bundle_seeded")),
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_hit_tokens": int(usage.get("cache_hit_tokens") or 0),
                "compression_marker_present": any(
                    TOKEN_COMPACT_MARKER in str(message.get("content") or "")
                    for message in request_messages
                ),
            }
        )
    rows.sort(key=lambda row: int(row.get("call_index") or 0))
    return rows


def _classify_node_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    classified: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for row in rows:
        reason = "steady_state"
        if previous is None:
            reason = "first_request"
        elif bool(row.get("compression_marker_present")):
            reason = "allowed_token_compression"
        elif int(row.get("request_message_count") or 0) < int(previous.get("request_message_count") or 0):
            reason = "potential_context_shrink"
        elif str(row.get("prompt_cache_key_hash") or "") != str(previous.get("prompt_cache_key_hash") or ""):
            reason = "family_reset"
        elif int(row.get("cache_hit_tokens") or 0) < int(previous.get("cache_hit_tokens") or 0):
            reason = "cache_hit_drop"
        classified.append({**row, "analysis_reason": reason})
        previous = row
    return classified


async def _run_node_acceptance(
    *,
    unlock_password: str,
    output_dir: Path,
    max_rounds: int,
    max_runtime_seconds: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if str(unlock_password or "").strip():
        _ensure_local_bootstrap_unlocked(password=str(unlock_password))
    original_cwd = Path.cwd()
    os.chdir(WORKSPACE_ROOT)
    try:
        config = load_config()
        isolated_runtime_root = _configure_isolated_main_runtime(config, session_id=NODE_SESSION_ID)
        provider = make_provider(config, scope="ceo")
        bus = MessageBus()
        loop = make_agent_loop(config, bus, provider, debug_mode=False)
        service = getattr(loop, "main_task_service", None)
        if service is None:
            raise RuntimeError("main_task_service is unavailable")

        pause_sent = False
        append_notice_sent = False
        append_notice_continued = False
        append_notice_baseline = 0

        try:
            await service.startup()
            record = await service.create_task(
                NODE_TASK_PROMPT,
                session_id=NODE_SESSION_ID,
                max_depth=0,
                metadata={
                    "core_requirement": NODE_TASK_PROMPT,
                    "contract_cache_acceptance": True,
                    "contract_cache_acceptance_created_at": datetime.now().isoformat(),
                },
            )
            start_time = time.monotonic()
            while True:
                task = service.get_task(record.task_id)
                root = service.get_node(record.root_node_id)
                rows = _classify_node_rows(_node_rows(service, record.task_id))
                model_call_count = len(rows)
                compression_triggered = any(bool(row.get("compression_marker_present")) for row in rows)
                task_paused = bool(getattr(task, "is_paused", False)) if task is not None else False
                task_status = str(getattr(task, "status", "") or "").strip().lower() if task is not None else ""

                if not pause_sent and compression_triggered and task_status == "in_progress" and not task_paused:
                    await service.pause_task(record.task_id)
                    pause_sent = True
                    await asyncio.sleep(1.0)
                    continue

                if pause_sent and not append_notice_sent and task_paused:
                    await service.task_append_notice(
                        task_ids=[record.task_id],
                        node_ids=[],
                        message=NODE_APPEND_NOTICE_PROMPT,
                        session_id=NODE_SESSION_ID,
                    )
                    append_notice_sent = True
                    append_notice_baseline = model_call_count
                    await asyncio.sleep(2.0)
                    continue

                if append_notice_sent and model_call_count > append_notice_baseline:
                    append_notice_continued = True

                if (
                    compression_triggered
                    and pause_sent
                    and append_notice_sent
                    and append_notice_continued
                ):
                    if task_status == "in_progress" and not task_paused:
                        await service.pause_task(record.task_id)
                        await asyncio.sleep(1.0)
                    break

                if model_call_count >= int(max_rounds) or time.monotonic() - start_time >= float(max_runtime_seconds):
                    if task_status == "in_progress" and not task_paused:
                        await service.pause_task(record.task_id)
                        await asyncio.sleep(1.0)
                    break

                await asyncio.sleep(2.0)

            task = service.get_task(record.task_id)
            root = service.get_node(record.root_node_id)
            rows = _classify_node_rows(_node_rows(service, record.task_id))
            children = service.store.list_children(record.root_node_id)
            report = {
                "task_id": record.task_id,
                "session_id": NODE_SESSION_ID,
                "isolated_runtime_root": str(isolated_runtime_root.resolve()),
                "root_node_id": record.root_node_id,
                "max_depth": int(getattr(task, "max_depth", 0) or 0),
                "row_count": len(rows),
                "task_status": str(getattr(task, "status", "") or "").strip(),
                "task_is_paused": bool(getattr(task, "is_paused", False)),
                "conditions": {
                    "compression_triggered": any(bool(row.get("compression_marker_present")) for row in rows),
                    "manual_pause_sent": pause_sent,
                    "append_notice_sent": append_notice_sent,
                    "append_notice_continued": append_notice_continued,
                    "root_can_spawn_children": bool(getattr(root, "can_spawn_children", True)),
                    "child_count": len(list(children or [])),
                },
                "rows": rows,
                "append_notice_message": NODE_APPEND_NOTICE_PROMPT,
                "stop_reason": (
                    "success"
                    if any(bool(row.get("compression_marker_present")) for row in rows)
                    and pause_sent
                    and append_notice_sent
                    and append_notice_continued
                    else "round_limit_or_timeout"
                ),
            }
            _persist_json(output_dir / "report.json", report)
            return report
        finally:
            await service.close()
            await loop.close_mcp()
    finally:
        os.chdir(original_cwd)


def _build_replay_notes(*, ceo_report: dict[str, Any], node_report: dict[str, Any]) -> str:
    ceo_rows = list(ceo_report.get("rows") or [])
    node_rows = list(node_report.get("rows") or [])
    lines = [
        "# Runtime Contract Acceptance Replay",
        "",
        "## CEO Path",
        f"- Session: `{ceo_report.get('session_id') or ''}`",
        f"- Stop reason: `{ceo_report.get('stop_reason') or ''}`",
        f"- Compression triggered: `{bool((ceo_report.get('conditions') or {}).get('compression_triggered'))}`",
        f"- Manual pause sent: `{bool((ceo_report.get('conditions') or {}).get('manual_pause_sent'))}`",
        f"- Progress reply observed: `{bool((ceo_report.get('conditions') or {}).get('progress_reply_observed'))}`",
        f"- Continue follow-ups observed: `{int((ceo_report.get('conditions') or {}).get('continue_follow_up_count') or 0)}`",
        "",
        "### CEO Token Replay",
    ]
    for row in ceo_rows:
        lines.append(
            "- "
            + (
                f"{row.get('request_id') or '<unknown>'}: lane={row.get('request_lane') or ''}, "
                f"input={int(row.get('input_tokens') or 0)}, cache_hit={int(row.get('cache_hit_tokens') or 0)}, "
                f"family={row.get('prompt_cache_key_hash') or ''}, stable_prefix={row.get('stable_prefix_hash') or ''}, "
                f"request={row.get('actual_request_hash') or ''}, shrink={row.get('frontdoor_history_shrink_reason') or ''}, "
                f"analysis={row.get('analysis_reason') or ''}"
            )
        )
    ceo_illegal_loss = [
        row
        for row in ceo_rows
        if str(row.get("analysis_reason") or "") == "potential_context_shrink"
        and str(row.get("frontdoor_history_shrink_reason") or "").strip() not in {"token_compression", "stage_compaction"}
        and str(row.get("request_lane") or "").strip() != "token_compression"
    ]
    lines.extend(
        [
            "",
            "## Node Path",
            f"- Task: `{node_report.get('task_id') or ''}`",
            f"- Stop reason: `{node_report.get('stop_reason') or ''}`",
            f"- Compression triggered: `{bool((node_report.get('conditions') or {}).get('compression_triggered'))}`",
            f"- Manual pause sent: `{bool((node_report.get('conditions') or {}).get('manual_pause_sent'))}`",
            f"- Append notice sent: `{bool((node_report.get('conditions') or {}).get('append_notice_sent'))}`",
            f"- Append notice continued: `{bool((node_report.get('conditions') or {}).get('append_notice_continued'))}`",
            f"- Root can spawn children: `{bool((node_report.get('conditions') or {}).get('root_can_spawn_children'))}`",
            f"- Child count: `{int((node_report.get('conditions') or {}).get('child_count') or 0)}`",
            "",
            "### Node Token Replay",
        ]
    )
    for row in node_rows:
        lines.append(
            "- "
            + (
                f"call={int(row.get('call_index') or 0)}: "
                f"input={int(row.get('input_tokens') or 0)}, cache_hit={int(row.get('cache_hit_tokens') or 0)}, "
                f"family={row.get('prompt_cache_key_hash') or ''}, request={row.get('actual_request_hash') or ''}, "
                f"message_count={int(row.get('request_message_count') or 0)}, "
                f"compression_marker={bool(row.get('compression_marker_present'))}, "
                f"analysis={row.get('analysis_reason') or ''}"
            )
        )
    node_illegal_loss = [
        row
        for row in node_rows
        if str(row.get("analysis_reason") or "") == "potential_context_shrink"
        and not bool(row.get("compression_marker_present"))
    ]
    lines.extend(
        [
            "",
            "## Replay Conclusions",
            f"- CEO suspected illegal context loss rows: `{len(ceo_illegal_loss)}`",
            f"- Node suspected illegal context loss rows: `{len(node_illegal_loss)}`",
            "- Cache-hit behavior should be reviewed row by row above together with `prompt_cache_key_hash`, `stable_prefix_hash`, and `actual_request_hash` changes.",
            "- If a shrink is marked as `allowed_token_compression` or the node request contains the token compact marker, treat that as allowed compaction rather than illegal context loss.",
        ]
    )
    return "\n".join(lines) + "\n"


async def _run_acceptance(
    *,
    host: str,
    port: int,
    unlock_password: str,
    output_root: Path,
    max_rounds: int,
    max_runtime_seconds: int,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    server_log = output_root / "web-server.log"
    server_log_handle = server_log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        _server_command(host=host, port=port),
        cwd=str(WORKSPACE_ROOT),
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
        ceo_report = await _run_ceo_acceptance(
            server=server,
            unlock_password=unlock_password,
            output_dir=output_root / "ceo",
            max_rounds=max_rounds,
            max_runtime_seconds=max_runtime_seconds,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
        server_log_handle.close()

    node_report = await _run_node_acceptance(
        unlock_password=unlock_password,
        output_dir=output_root / "node",
        max_rounds=max_rounds,
        max_runtime_seconds=max_runtime_seconds,
    )

    combined = {
        "captured_at": datetime.now().isoformat(),
        "output_root": str(output_root.resolve()),
        "server_log": str(server_log.resolve()),
        "ceo": ceo_report,
        "node": node_report,
    }
    notes = _build_replay_notes(ceo_report=ceo_report, node_report=node_report)
    _persist_json(output_root / "combined-report.json", combined)
    (output_root / "replay-notes.md").write_text(notes, encoding="utf-8")
    return combined


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run runtime-contract acceptance for CEO and node flows.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18797)
    parser.add_argument("--unlock-password", default="qaz")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    parser.add_argument("--max-runtime-seconds", type=int, default=DEFAULT_MAX_RUNTIME_SECONDS)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / ".g3ku" / "runtime-contract-acceptance" / datetime.now().strftime("%Y%m%dT%H%M%S"),
    )
    return parser


async def _async_main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    combined = await _run_acceptance(
        host=str(args.host),
        port=int(args.port),
        unlock_password=str(args.unlock_password),
        output_root=Path(args.output_root),
        max_rounds=int(args.max_rounds),
        max_runtime_seconds=int(args.max_runtime_seconds),
    )
    print(json.dumps(combined, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
