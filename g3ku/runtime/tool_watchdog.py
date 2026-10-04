from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


def _env_default_tool_timeout_seconds() -> float:
    raw = str(os.environ.get("G3KU_TOOL_DEFAULT_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 600.0
    try:
        parsed = float(raw)
    except ValueError:
        return 600.0
    return max(1.0, parsed)


# 全局工具调用硬上限默认值（秒）：所有工具的最大运行时长保底，显式传入的
# timeout_seconds 参数优先；无上限约束（调用方可传任意更大的值）。
# 参数名带单位（_seconds）以杜绝把秒误当毫秒（历史事故：模型传 60000 想要
# 60s，实际是 60000s≈16.6h，卡死工具并把失速截止一并推到 16h 后）。
# 下限支持真正的亚秒（0.05s），与 watchdog 轮询粒度对齐。
DEFAULT_TOOL_TIMEOUT_SECONDS: float = _env_default_tool_timeout_seconds()
MIN_TOOL_TIMEOUT_SECONDS: float = 0.05
TOOL_TIMEOUT_ARGUMENT_NAME = "timeout_seconds"


@dataclass(slots=True)
class ToolWatchdogConfig:
    enabled: bool = True
    poll_interval_seconds: float = 5.0
    stop_grace_seconds: float = 2.0
    text_char_limit: int = 280
    list_limit: int = 3
    default_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS


@dataclass(slots=True)
class ToolWatchdogRunResult:
    completed: bool
    value: Any
    elapsed_seconds: float
    poll_count: int
    snapshot: dict[str, Any] | None = None
    execution_id: str = ""
    timed_out: bool = False


def runtime_context_value(runtime_context: Any, key: str, default: Any = None) -> Any:
    if isinstance(runtime_context, dict):
        return runtime_context.get(key, default)
    return getattr(runtime_context, key, default)


def tool_arguments_request_timeout_budget(arguments: dict[str, Any] | None) -> bool:
    if not isinstance(arguments, dict):
        return False
    for raw_key, value in arguments.items():
        key = str(raw_key or "").strip().lower()
        if "timeout" not in key:
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, set, dict)) and not value:
            continue
        return True
    return False


def actor_role_allows_watchdog(runtime_context: Any) -> bool:
    role = str(runtime_context_value(runtime_context, "actor_role", "") or "").strip().lower()
    # CEO and execution/acceptance nodes all benefit from watchdog polling so long
    # tools remain interruptible. Acceptance nodes carry the node-side actor role
    # "inspection", so it must stay in this allow set for them to be covered.
    # Whether the poll loop is allowed to detach into a background handoff is a
    # separate decision.
    return role in {"ceo", "execution", "acceptance", "inspection"}




def resolve_tool_watchdog_config(runtime_context: Any) -> ToolWatchdogConfig:
    raw = runtime_context_value(runtime_context, "tool_watchdog", None)
    payload = dict(raw) if isinstance(raw, dict) else {}
    enabled = payload.get("enabled", True)
    poll_interval = payload.get("poll_interval_seconds", 5.0)
    stop_grace = payload.get("stop_grace_seconds", payload.get("cancel_grace_seconds", 2.0))
    text_char_limit = payload.get("text_char_limit", 280)
    list_limit = payload.get("list_limit", 3)
    default_timeout = payload.get("default_timeout_seconds", DEFAULT_TOOL_TIMEOUT_SECONDS)
    return ToolWatchdogConfig(
        enabled=bool(enabled),
        poll_interval_seconds=max(0.2, float(poll_interval or 5.0)),
        stop_grace_seconds=max(0.0, float(stop_grace or 0.0)),
        text_char_limit=max(80, int(text_char_limit or 280)),
        list_limit=max(1, int(list_limit or 3)),
        default_timeout_seconds=max(MIN_TOOL_TIMEOUT_SECONDS, float(default_timeout or DEFAULT_TOOL_TIMEOUT_SECONDS)),
    )


def coerce_timeout_argument(value: Any) -> float | None:
    """把调用方传入的 timeout_seconds 参数归一成秒数；无效/缺省返回 None（交给全局默认）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
    else:
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):  # NaN/inf 防御
        return None
    if parsed < MIN_TOOL_TIMEOUT_SECONDS:
        return MIN_TOOL_TIMEOUT_SECONDS
    return parsed


def resolve_effective_tool_timeout(arguments: dict[str, Any] | None, runtime_context: Any) -> float:
    """统一解析一次工具调用的最大运行时长：显式传参 > 全局默认（无上限）。"""
    explicit = coerce_timeout_argument(
        (arguments or {}).get(TOOL_TIMEOUT_ARGUMENT_NAME) if isinstance(arguments, dict) else None
    )
    if explicit is not None:
        return explicit
    return resolve_tool_watchdog_config(runtime_context).default_timeout_seconds


def build_tool_timeout_error_text(*, tool_name: str, timeout_seconds: float) -> str:
    """工具超时被停止后返回给模型的统一错误文案（含如何延长的指引）。"""
    normalized_tool_name = str(tool_name or "tool").strip() or "tool"
    seconds = max(0.0, float(timeout_seconds or 0.0))
    # 亚秒上限要显示出小数，否则 0.3s 会渲染成 "0s" 误导模型。
    seconds_text = f"{seconds:.2f}".rstrip("0").rstrip(".") if seconds < 1 else f"{seconds:.0f}"
    return (
        f"Error executing {normalized_tool_name}: timed out after {seconds_text}s. "
        f"该工具调用因超出 {seconds_text}s 运行时长上限被停止。"
        f"如果它确实需要更长时间，请在下一次调用时显式传入更大的 \"timeout_seconds\" 参数（单位：秒，支持小数）。"
    )


def resolve_snapshot_supplier(runtime_context: Any) -> Callable[[], Any] | None:
    supplier = runtime_context_value(runtime_context, "tool_snapshot_supplier", None)
    return supplier if callable(supplier) else None



async def request_tool_cancellation(
    execution_task: asyncio.Task[Any],
    *,
    cancel_token: Any | None,
    reason: str,
    grace_seconds: float,
) -> None:
    if cancel_token is not None and hasattr(cancel_token, "cancel"):
        try:
            cancel_token.cancel(reason=reason)
        except Exception:
            pass

    if execution_task.done():
        return

    if grace_seconds > 0:
        try:
            await asyncio.wait_for(asyncio.shield(execution_task), timeout=grace_seconds)
            return
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            return
        except Exception:
            return

    execution_task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(execution_task), timeout=max(0.1, grace_seconds))
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        return


def summarize_runtime_snapshot(
    payload: Any,
    *,
    text_char_limit: int = 280,
    list_limit: int = 3,
) -> dict[str, Any] | None:
    if payload is None:
        return {
            "snapshot_type": "empty",
            "summary_text": "当前还没有可用的运行快照。",
        }

    if isinstance(payload, dict) and isinstance(payload.get("task"), dict) and (
        isinstance(payload.get("root_node"), dict) or isinstance(payload.get("frontier"), list)
    ):
        task = payload["task"]
        root_node = payload.get("root_node") if isinstance(payload.get("root_node"), dict) else {}
        frontier = [item for item in list(payload.get("frontier") or []) if isinstance(item, dict)]
        preferred_node_id = str((frontier[0] if frontier else {}).get("node_id") or root_node.get("node_id") or "").strip()
        live_state = {"frames": frontier}
        execution_trace = root_node.get("execution_trace") if isinstance(root_node.get("execution_trace"), dict) else {}
        tool_steps = [item for item in list(execution_trace.get("tool_steps") or []) if isinstance(item, dict)]
        if not tool_steps:
            tool_steps = _runtime_summary_tool_steps(live_state)
        recent_tools = [
            {
                "tool_name": str(item.get("tool_name") or "tool"),
                "status": str(item.get("status") or "unknown"),
                "output_text": _clip_text(item.get("output_text", ""), limit=text_char_limit // 2),
            }
            for item in tool_steps[-list_limit:]
            if isinstance(item, dict)
        ]
        latest_summary_source = (
            root_node.get("final_output")
            or root_node.get("output")
            or root_node.get("failure_reason")
            or str((frontier[0] if frontier else {}).get("stage_goal") or "")
            or _runtime_summary_tool_calls_summary(
                live_state,
                preferred_node_id=preferred_node_id,
                limit=list_limit,
            )
            or _tool_steps_summary(tool_steps, limit=list_limit)
        )
        latest_summary = _clip_text(
            latest_summary_source,
            limit=text_char_limit,
        )
        latest_node = {
            "title": root_node.get("goal") or root_node.get("title") or "",
            "node_id": root_node.get("node_id") or preferred_node_id,
            "updated_at": root_node.get("updated_at") or "",
            "status": root_node.get("status") or task.get("status") or "in_progress",
        }
        root = {"goal": root_node.get("goal") or ""}
        node_title = str(latest_node.get("title") or root.get("goal") or latest_node.get("node_id") or "当前节点")
        node_status = str(root_node.get("status") or task.get("status") or "in_progress")
        summary_text = f"任务仍在进行中；最近节点“{node_title}”状态为 {node_status}。"
        if latest_summary:
            summary_text = f"{summary_text} 最近输出：{latest_summary}"
        return {
            "snapshot_type": "main_task_detail",
            "summary_text": summary_text,
            "task_id": str(task.get("task_id") or ""),
            "task_status": str(task.get("status") or ""),
            "updated_at": str(task.get("updated_at") or ""),
            "latest_node": {
                "node_id": str(latest_node.get("node_id") or ""),
                "status": node_status,
                "title": node_title,
                "updated_at": str(latest_node.get("updated_at") or ""),
                "output_excerpt": latest_summary,
            },
            "recent_tool_steps": recent_tools,
        }

    if isinstance(payload, dict) and ("tool_events" in payload or "assistant_text" in payload or "status" in payload):
        tool_events = [item for item in list(payload.get("tool_events") or []) if isinstance(item, dict)]
        recent_events = [
            {
                "tool_name": str(item.get("tool_name") or "tool"),
                "status": str(item.get("status") or "running"),
                "text": _clip_text(item.get("text", ""), limit=text_char_limit // 2),
            }
            for item in tool_events[-list_limit:]
        ]
        latest_event = recent_events[-1] if recent_events else {}
        latest_text = str(latest_event.get("text") or "")
        assistant_text = _clip_text(payload.get("assistant_text", ""), limit=text_char_limit // 2)
        summary_text = f"会话仍在运行，最近阶段状态为 {str(payload.get('status') or 'running')}。"
        if latest_text:
            summary_text = f"{summary_text} 最近进度：{latest_text}"
        elif assistant_text:
            summary_text = f"{summary_text} 助手最近输出：{assistant_text}"
        return {
            "snapshot_type": "ceo_inflight_turn",
            "summary_text": summary_text,
            "status": str(payload.get("status") or "running"),
            "assistant_text_excerpt": assistant_text,
            "recent_tool_events": recent_events,
            "last_error": _compact_error(payload.get("last_error")),
        }

    if isinstance(payload, dict):
        compact: dict[str, Any] = {}
        for key, value in list(payload.items())[:list_limit]:
            if isinstance(value, (str, int, float, bool)) or value is None:
                compact[str(key)] = value
            elif isinstance(value, dict):
                compact[str(key)] = {
                    str(inner_key): inner_value
                    for inner_key, inner_value in list(value.items())[:list_limit]
                    if isinstance(inner_value, (str, int, float, bool)) or inner_value is None
                }
            elif isinstance(value, list):
                compact[str(key)] = [_clip_text(item, limit=text_char_limit // 4) for item in value[:list_limit]]
            else:
                compact[str(key)] = _clip_text(value, limit=text_char_limit // 4)
        return {
            "snapshot_type": "generic",
            "summary_text": _clip_text(json.dumps(compact, ensure_ascii=False), limit=text_char_limit),
            "payload": compact,
        }

    return {
        "snapshot_type": "scalar",
        "summary_text": _clip_text(payload, limit=text_char_limit),
    }



def _progress_tool_steps(progress: Any, *, preferred_node_id: Any = '') -> list[dict[str, Any]]:
    if not isinstance(progress, dict):
        return []
    preferred = str(preferred_node_id or '').strip()
    latest_node = progress.get("latest_node") if isinstance(progress.get("latest_node"), dict) else {}
    execution_trace = latest_node.get("execution_trace") if isinstance(latest_node, dict) else None
    if isinstance(execution_trace, dict):
        tool_steps = [item for item in list(execution_trace.get("tool_steps") or []) if isinstance(item, dict)]
        if tool_steps:
            return tool_steps
    selected = None
    for item in list(progress.get("nodes") or []):
        if not isinstance(item, dict):
            continue
        if preferred and str(item.get("node_id") or "").strip() != preferred:
            continue
        selected = item
        break
    if selected is None and isinstance(latest_node, dict):
        latest_node_id = str(latest_node.get("node_id") or "").strip()
        if latest_node_id:
            selected = next(
                (
                    item
                    for item in list(progress.get("nodes") or [])
                    if isinstance(item, dict) and str(item.get("node_id") or "").strip() == latest_node_id
                ),
                None,
            )
    if selected is None:
        selected = next((item for item in list(progress.get("nodes") or []) if isinstance(item, dict)), None)
    if not isinstance(selected, dict):
        return []
    execution_trace = selected.get("execution_trace") if isinstance(selected.get("execution_trace"), dict) else None
    if not isinstance(execution_trace, dict):
        return []
    return [item for item in list(execution_trace.get("tool_steps") or []) if isinstance(item, dict)]


def _tool_steps_summary(tool_steps: list[dict[str, Any]], *, limit: int = 3) -> str:
    steps = [item for item in list(tool_steps or []) if isinstance(item, dict) and str(item.get("tool_name") or "").strip()]
    if not steps:
        return ""
    lines = ["Recent tool calls:"]
    for item in steps[-max(1, int(limit or 1)) :]:
        tool_name = str(item.get("tool_name") or "tool").strip() or "tool"
        status = str(item.get("status") or "queued").strip() or "queued"
        lines.append(f"- {tool_name} [{status}]")
    return "\n".join(lines)


def _runtime_summary_tool_steps(runtime_summary: Any) -> list[dict[str, Any]]:
    if not isinstance(runtime_summary, dict):
        return []
    collected: list[dict[str, Any]] = []
    for frame in list(runtime_summary.get("frames") or []):
        if not isinstance(frame, dict):
            continue
        for item in list(frame.get("tool_calls") or []):
            if not isinstance(item, dict):
                continue
            collected.append(
                {
                    "tool_name": str(item.get("tool_name") or "tool"),
                    "status": str(item.get("status") or "unknown"),
                    "output_text": "",
                }
            )
    return collected


def _runtime_summary_tool_calls_summary(
    runtime_summary: Any,
    *,
    preferred_node_id: Any = "",
    limit: int = 3,
) -> str:
    if not isinstance(runtime_summary, dict):
        return ""
    frames = [item for item in list(runtime_summary.get("frames") or []) if isinstance(item, dict)]
    if not frames:
        return ""
    selected = None
    preferred = str(preferred_node_id or "").strip()
    if preferred:
        selected = next((frame for frame in frames if str(frame.get("node_id") or "").strip() == preferred), None)
    if selected is None:
        frames_by_node = {str(frame.get("node_id") or "").strip(): frame for frame in frames if str(frame.get("node_id") or "").strip()}
        for node_id in [
            *list(runtime_summary.get("active_node_ids") or []),
            *list(runtime_summary.get("runnable_node_ids") or []),
            *list(runtime_summary.get("waiting_node_ids") or []),
        ]:
            selected = frames_by_node.get(str(node_id or "").strip())
            if selected is not None:
                break
    if selected is None:
        selected = frames[0]
    lines: list[str] = []
    tool_calls = [item for item in list(selected.get("tool_calls") or []) if isinstance(item, dict) and str(item.get("tool_name") or "").strip()]
    if tool_calls:
        lines.append("Recent tool calls:")
        for item in tool_calls[-max(1, int(limit or 1)) :]:
            tool_name = str(item.get("tool_name") or "tool").strip() or "tool"
            status = str(item.get("status") or "queued").strip() or "queued"
            lines.append(f"- {tool_name} [{status}]")
    return "\n".join(lines)


async def run_tool_with_watchdog(
    awaitable: Awaitable[Any],
    *,
    tool_name: str,
    arguments: dict[str, Any],
    runtime_context: Any,
    snapshot_supplier: Callable[[], Any] | None = None,
    on_poll: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    inline_registry: Any | None = None,
    on_inline_registered: Callable[[Any], Awaitable[None] | None] | None = None,
    hard_timeout_seconds: float | None = None,
    universal_timeout_exempt: bool = False,
) -> ToolWatchdogRunResult:
    config = resolve_tool_watchdog_config(runtime_context)
    if not config.enabled:
        value = await awaitable
        return ToolWatchdogRunResult(
            completed=True,
            value=value,
            elapsed_seconds=0.0,
            poll_count=0,
            snapshot=None,
            execution_id="",
        )

    supplier = snapshot_supplier or resolve_snapshot_supplier(runtime_context)
    execution_task = asyncio.create_task(awaitable, name=f"tool-watchdog:{tool_name}")
    cancel_token = runtime_context_value(runtime_context, "cancel_token", None)
    session_key = str(runtime_context_value(runtime_context, "session_key", "") or "").strip()
    runtime_session = runtime_context_value(runtime_context, "runtime_session", None)
    started_at = time.monotonic()
    inline_entry = None

    try:
        if inline_registry is not None and hasattr(inline_registry, "register_execution"):
            inline_entry = await inline_registry.register_execution(
                session_key=session_key,
                turn_id=str(runtime_context_value(runtime_context, "turn_id", "") or "").strip(),
                tool_name=tool_name,
                tool_call_id=str(runtime_context_value(runtime_context, "tool_call_id", "") or "").strip(),
                arguments=dict(arguments or {}),
                task=execution_task,
                snapshot_supplier=supplier,
                cancel_token=cancel_token,
                started_at=started_at,
                runtime_session=runtime_session,
                # 自持工具的硬上限在工具内部，取统一解析值供巡检判定参考；
                # 豁免工具（exempt_universal_timeout）没有任何外层时限，
                # 登记为 None，避免提醒侧车道向模型宣称不存在的运行上限。
                timeout_seconds=(
                    None
                    if universal_timeout_exempt
                    else (
                        hard_timeout_seconds
                        if hard_timeout_seconds
                        else resolve_effective_tool_timeout(arguments, runtime_context)
                    )
                ),
            )
            if on_inline_registered is not None:
                await _maybe_await(on_inline_registered(inline_entry))
        result = await _wait_for_task_window(
            task=execution_task,
            tool_name=tool_name,
            started_at=started_at,
            snapshot_supplier=supplier,
            poll_interval_seconds=config.poll_interval_seconds,
            handoff_after_seconds=10_000_000.0,
            text_char_limit=config.text_char_limit,
            list_limit=config.list_limit,
            on_poll=on_poll,
            hard_timeout_seconds=hard_timeout_seconds,
            cancel_token=cancel_token,
        )
        if result.timed_out:
            return ToolWatchdogRunResult(
                completed=True,
                value=build_tool_timeout_error_text(
                    tool_name=tool_name,
                    timeout_seconds=hard_timeout_seconds or config.default_timeout_seconds,
                ),
                elapsed_seconds=result.elapsed_seconds,
                poll_count=result.poll_count,
                snapshot=result.snapshot,
                execution_id="",
                timed_out=True,
            )
        return ToolWatchdogRunResult(
            completed=True,
            value=result.value,
            elapsed_seconds=result.elapsed_seconds,
            poll_count=result.poll_count,
            snapshot=result.snapshot,
            execution_id="",
        )
    except BaseException:
        if not execution_task.done():
            await request_tool_cancellation(
                execution_task,
                cancel_token=cancel_token,
                reason=f"watchdog_aborted:{tool_name}",
                grace_seconds=config.stop_grace_seconds,
            )
        raise


async def _enforce_hard_timeout(*, task: asyncio.Task[Any], cancel_token: Any | None, tool_name: str) -> None:
    """到点后无条件终止工具任务：先走取消令牌级联（杀已注册进程），再硬取消协程。"""
    await request_tool_cancellation(
        task,
        cancel_token=cancel_token,
        reason=f"tool_timeout:{tool_name}",
        grace_seconds=0.0,
    )


async def run_tool_with_hard_timeout(
    awaitable: Awaitable[Any],
    *,
    tool_name: str,
    timeout_seconds: float,
    cancel_token: Any | None = None,
) -> Any:
    """不走 watchdog 轮询的薄包装：到点硬超时并返回统一超时错误文案。"""
    execution_task = asyncio.create_task(awaitable, name=f"tool-hard-timeout:{tool_name}")
    try:
        return await asyncio.wait_for(asyncio.shield(execution_task), timeout=max(MIN_TOOL_TIMEOUT_SECONDS, float(timeout_seconds)))
    except asyncio.TimeoutError:
        await _enforce_hard_timeout(task=execution_task, cancel_token=cancel_token, tool_name=tool_name)
        return build_tool_timeout_error_text(tool_name=tool_name, timeout_seconds=timeout_seconds)
    except BaseException:
        if not execution_task.done():
            execution_task.cancel()
        raise


async def _safe_summarized_snapshot(
    *,
    snapshot_supplier: Callable[[], Any] | None,
    tool_name: str,
    text_char_limit: int,
    list_limit: int,
    fallback: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """快照采集是只读观测旁路：采集失败降级为旧快照，绝不向上传播。

    快照异常若穿透到 run_tool_with_watchdog 的 except BaseException 分支，
    会触发 request_tool_cancellation 误杀仍在执行的长时工具。此处捕获采集
    异常并回退最近一次有效快照，仅记录告警；取消类异常不在此拦截范围内。
    """
    try:
        payload = await _maybe_await_callable(snapshot_supplier)
    except Exception:
        logger.warning(
            "tool watchdog snapshot supplier failed; keeping previous snapshot (tool=%s)",
            tool_name,
            exc_info=True,
        )
        return fallback
    return summarize_runtime_snapshot(
        payload,
        text_char_limit=text_char_limit,
        list_limit=list_limit,
    )


async def _wait_for_task_window(
    *,
    task: asyncio.Task[Any],
    tool_name: str,
    started_at: float,
    snapshot_supplier: Callable[[], Any] | None,
    poll_interval_seconds: float,
    handoff_after_seconds: float,
    text_char_limit: int,
    list_limit: int,
    on_poll: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    hard_timeout_seconds: float | None = None,
    cancel_token: Any | None = None,
) -> ToolWatchdogRunResult:
    poll_count = 0
    last_snapshot: dict[str, Any] | None = None
    deadline = time.monotonic() + max(0.1, float(handoff_after_seconds))
    hard_deadline = (
        float(started_at) + max(MIN_TOOL_TIMEOUT_SECONDS, float(hard_timeout_seconds))
        if hard_timeout_seconds
        else None
    )
    while True:
        if hard_deadline is not None and time.monotonic() >= hard_deadline:
            await _enforce_hard_timeout(task=task, cancel_token=cancel_token, tool_name=tool_name)
            return ToolWatchdogRunResult(
                completed=True,
                value=None,
                elapsed_seconds=max(0.0, time.monotonic() - started_at),
                poll_count=poll_count,
                snapshot=last_snapshot,
                timed_out=True,
            )
        remaining_to_handoff = deadline - time.monotonic()
        if remaining_to_handoff <= 0:
            snapshot = await _safe_summarized_snapshot(
                snapshot_supplier=snapshot_supplier,
                tool_name=tool_name,
                text_char_limit=text_char_limit,
                list_limit=list_limit,
                fallback=last_snapshot,
            )
            elapsed = max(0.0, time.monotonic() - started_at)
            return ToolWatchdogRunResult(
                completed=False,
                value=None,
                elapsed_seconds=elapsed,
                poll_count=poll_count,
                snapshot=snapshot or last_snapshot,
            )

        wait_timeout = min(max(0.05, float(poll_interval_seconds)), remaining_to_handoff)
        if hard_deadline is not None:
            wait_timeout = min(wait_timeout, max(0.05, hard_deadline - time.monotonic()))
        try:
            value = await asyncio.wait_for(asyncio.shield(task), timeout=wait_timeout)
            elapsed = max(0.0, time.monotonic() - started_at)
            return ToolWatchdogRunResult(
                completed=True,
                value=value,
                elapsed_seconds=elapsed,
                poll_count=poll_count,
                snapshot=last_snapshot,
            )
        except asyncio.TimeoutError:
            poll_count += 1
            last_snapshot = await _safe_summarized_snapshot(
                snapshot_supplier=snapshot_supplier,
                tool_name=tool_name,
                text_char_limit=text_char_limit,
                list_limit=list_limit,
                fallback=last_snapshot,
            )
            if on_poll is not None:
                elapsed = max(0.0, time.monotonic() - started_at)
                await _maybe_await(
                    on_poll(
                        {
                            "tool_name": str(tool_name or "tool"),
                            "elapsed_seconds": round(elapsed, 1),
                            "poll_count": poll_count,
                            "snapshot": last_snapshot,
                            "next_handoff_in_seconds": round(max(0.0, deadline - time.monotonic()), 1),
                        }
                    )
                )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except Exception:
        return str(value)




async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _maybe_await_callable(callback: Callable[[], Any] | None) -> Any:
    if callback is None:
        return None
    return await _maybe_await(callback())


def _clip_text(value: Any, *, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}..."


def _compact_error(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return {
            "code": str(value.get("code") or ""),
            "message": _clip_text(value.get("message", ""), limit=180),
        }
    return None


