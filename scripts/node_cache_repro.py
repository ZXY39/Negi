from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.bus.queue import MessageBus
from g3ku.config.loader import load_config
from g3ku.runtime.bootstrap_factory import make_agent_loop, make_provider
from g3ku.security.bootstrap import get_bootstrap_security_service

TEST_PROMPT = """在已加载并遵循相关skills的前提下，完成一份截至今天为止的过去一年全球范围二次元女角色热度榜单Top20。重点关注讨论度、社媒热度、趋势信号，而非仅按传统投票人气排序。范围应覆盖漫改动画角色、VTuber/虚拟偶像系角色，并允许纳入其他广义二次元女性角色，但必须明确纳入标准与边界。交付物：1）Excel榜单文件，字段至少包含排名、角色名、作品/来源、人设简介、配音/声优、代表名台词、头像/封面图链接或来源、热度依据、主要来源链接、备注；2）文字总结，概述总体趋势、类型分布、上榜原因、地区差异与局限。资料应基于公开可验证来源，必要时可结合多搜索引擎与网页抓取技能/工具进行核实，并在成品中标明统计口径、截至日期与局限性。"""


def _primary_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage_by_model = list(payload.get("delta_usage_by_model") or [])
    if not usage_by_model:
        return {}
    first = usage_by_model[0]
    return dict(first or {}) if isinstance(first, dict) else {}


def _safe_runtime_suffix(session_id: str) -> str:
    raw = str(session_id or "").strip() or datetime.now().strftime("%Y%m%d%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in raw)
    return safe[:80] or datetime.now().strftime("%Y%m%d%H%M%S")


def _configure_isolated_main_runtime(config, *, session_id: str) -> Path:
    runtime_root = REPO_ROOT / ".g3ku" / "cache-repro-runtime" / _safe_runtime_suffix(session_id)
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


def _rows_from_model_calls(model_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in reversed(list(model_calls or [])):
        payload = dict(item.get("payload") or {})
        usage = _primary_usage(payload)
        rows.append(
            {
                "seq": item.get("seq"),
                "created_at": item.get("created_at"),
                "node_id": item.get("node_id"),
                "call_index": payload.get("call_index"),
                "prompt_cache_key_hash": payload.get("prompt_cache_key_hash"),
                "actual_request_hash": payload.get("actual_request_hash"),
                "actual_tool_schema_hash": payload.get("actual_tool_schema_hash"),
                "actual_request_ref": payload.get("actual_request_ref"),
                "callable_tool_names": list(payload.get("callable_tool_names") or []),
                "provider_tool_names": list(payload.get("provider_tool_names") or []),
                "provider_tool_bundle_seeded": bool(payload.get("provider_tool_bundle_seeded")),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_hit_tokens": usage.get("cache_hit_tokens"),
                "cache_write_tokens": usage.get("cache_write_tokens"),
                "cache_hit_ratio": (
                    round(float(usage.get("cache_hit_tokens") or 0) / float(usage.get("input_tokens") or 1), 6)
                    if float(usage.get("input_tokens") or 0) > 0
                    else 0.0
                ),
            }
        )
    return rows


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    transitions: list[dict[str, Any]] = []
    zero_hit_transitions = 0
    for previous, current in zip(rows, rows[1:]):
        if previous.get("actual_tool_schema_hash") == current.get("actual_tool_schema_hash"):
            continue
        transition = {
            "from_call_index": previous.get("call_index"),
            "to_call_index": current.get("call_index"),
            "from_schema": previous.get("actual_tool_schema_hash"),
            "to_schema": current.get("actual_tool_schema_hash"),
            "from_provider_tool_names": list(previous.get("provider_tool_names") or []),
            "to_provider_tool_names": list(current.get("provider_tool_names") or []),
            "to_cache_hit_tokens": current.get("cache_hit_tokens"),
        }
        if int(current.get("cache_hit_tokens") or 0) == 0:
            zero_hit_transitions += 1
        transitions.append(transition)
    return {
        "call_count": len(rows),
        "cache_hit_call_count": sum(1 for row in rows if int(row.get("cache_hit_tokens") or 0) > 0),
        "zero_cache_hit_call_count": sum(1 for row in rows if int(row.get("cache_hit_tokens") or 0) == 0),
        "overall_cache_hit_ratio": (
            round(
                sum(float(row.get("cache_hit_tokens") or 0) for row in rows)
                / max(1.0, sum(float(row.get("input_tokens") or 0) for row in rows)),
                6,
            )
            if rows
            else 0.0
        ),
        "steady_state_cache_hit_ratio_calls_ge_4": (
            round(
                sum(float(row.get("cache_hit_tokens") or 0) for row in rows if int(row.get("call_index") or 0) >= 4)
                / max(1.0, sum(float(row.get("input_tokens") or 0) for row in rows if int(row.get("call_index") or 0) >= 4)),
                6,
            )
            if any(int(row.get("call_index") or 0) >= 4 for row in rows)
            else 0.0
        ),
        "steady_state_cache_hit_ratio_calls_ge_6": (
            round(
                sum(float(row.get("cache_hit_tokens") or 0) for row in rows if int(row.get("call_index") or 0) >= 6)
                / max(1.0, sum(float(row.get("input_tokens") or 0) for row in rows if int(row.get("call_index") or 0) >= 6)),
                6,
            )
            if any(int(row.get("call_index") or 0) >= 6 for row in rows)
            else 0.0
        ),
        "schema_transition_count": len(transitions),
        "zero_hit_transition_count": zero_hit_transitions,
        "schema_transitions": transitions,
    }


async def _wait_for_repro_window(
    service,
    task_id: str,
    *,
    max_wait_seconds: int,
    idle_seconds: int,
    min_call_count: int,
) -> tuple[Any, str]:
    started = time.monotonic()
    last_call_count = -1
    last_change_at = started
    while True:
        task = service.get_task(task_id)
        calls = service.store.list_task_model_calls(task_id, limit=500)
        call_count = len(list(calls or []))
        now = time.monotonic()
        if call_count != last_call_count:
            last_call_count = call_count
            last_change_at = now
        task_status = str(getattr(task, "status", "") or "").strip().lower()
        if task_status in {"success", "failed"} or bool(getattr(task, "is_paused", False)):
            return task, "terminal"
        if call_count >= int(min_call_count) and (now - last_change_at) >= float(idle_seconds):
            return task, "idle_window"
        if (now - started) >= float(max_wait_seconds):
            return task, "timeout"
        await asyncio.sleep(2.0)


async def _run_repro(
    *,
    session_id: str,
    max_depth: int,
    max_iterations: int,
    max_wait_seconds: int,
    idle_seconds: int,
    min_call_count: int,
    unlock_password: str,
) -> dict[str, Any]:
    if str(unlock_password or "").strip():
        get_bootstrap_security_service(REPO_ROOT).unlock(password=str(unlock_password))
    config = load_config()
    isolated_runtime_root = _configure_isolated_main_runtime(config, session_id=session_id)
    provider = make_provider(config, scope="ceo")
    bus = MessageBus()
    loop = make_agent_loop(config, bus, provider, debug_mode=False)
    service = getattr(loop, "main_task_service", None)
    if service is None:
        raise RuntimeError("main_task_service is unavailable")

    try:
        await service.startup()
        service.node_runner._execution_max_iterations = int(max_iterations)
        service.node_runner._acceptance_max_iterations = int(max_iterations)

        record = await service.create_task(
            TEST_PROMPT,
            session_id=session_id,
            max_depth=int(max_depth),
            metadata={
                "cache_repro": True,
                "cache_repro_created_at": datetime.now().isoformat(),
            },
        )
        final_task, wait_reason = await _wait_for_repro_window(
            service,
            record.task_id,
            max_wait_seconds=int(max_wait_seconds),
            idle_seconds=int(idle_seconds),
            min_call_count=int(min_call_count),
        )
        if (
            final_task is not None
            and str(getattr(final_task, "status", "") or "").strip().lower() == "in_progress"
            and not bool(getattr(final_task, "is_paused", False))
        ):
            await service.pause_task(record.task_id)
            await asyncio.sleep(1.0)
            final_task = service.get_task(record.task_id)
        model_calls = service.store.list_task_model_calls(record.task_id, limit=500)
        rows = _rows_from_model_calls(model_calls)
        latest_context = service.get_node_latest_context_payload(record.task_id, record.root_node_id)
        return {
            "task_id": record.task_id,
            "session_id": session_id,
            "isolated_runtime_root": str(isolated_runtime_root),
            "requested_max_depth": int(max_depth),
            "requested_max_iterations": int(max_iterations),
            "wait_reason": wait_reason,
            "requested_max_wait_seconds": int(max_wait_seconds),
            "requested_idle_seconds": int(idle_seconds),
            "requested_min_call_count": int(min_call_count),
            "task_status": str(getattr(final_task, "status", "") or ""),
            "task_is_paused": bool(getattr(final_task, "is_paused", False)),
            "root_node_id": record.root_node_id,
            "rows": rows,
            "summary": _summarize(rows),
            "latest_context": latest_context,
        }
    finally:
        await service.close()
        await loop.close_mcp()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a real async task and dump node cache diagnostics.")
    parser.add_argument("--session-id", default=f"web:cache-repro:{datetime.now().strftime('%Y%m%d%H%M%S')}")
    parser.add_argument("--max-depth", type=int, default=0)
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--max-wait-seconds", type=int, default=360)
    parser.add_argument("--idle-seconds", type=int, default=20)
    parser.add_argument("--min-call-count", type=int, default=3)
    parser.add_argument("--unlock-password", default="")
    parser.add_argument("--output", type=Path, default=None)
    return parser


async def _async_main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    payload = await _run_repro(
        session_id=str(args.session_id),
        max_depth=int(args.max_depth),
        max_iterations=int(args.max_iterations),
        max_wait_seconds=int(args.max_wait_seconds),
        idle_seconds=int(args.idle_seconds),
        min_call_count=int(args.min_call_count),
        unlock_password=str(args.unlock_password or ""),
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
