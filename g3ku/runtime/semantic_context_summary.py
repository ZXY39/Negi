from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from g3ku.runtime.context.summarizer import estimate_tokens, truncate_by_tokens

G3KU_MIN_CONTEXT_FLOOR = 20_000
HERMES_TRIGGER_RATIO = 0.10
HERMES_TARGET_RATIO = 0.20
HERMES_MIN_OUTPUT_TOKENS = 2000
HERMES_MAX_OUTPUT_RATIO = 0.05
HERMES_MAX_OUTPUT_TOKENS_CEILING = 12_000
HERMES_PRESSURE_WARN_RATIO = 0.85
HERMES_FORCE_REFRESH_RATIO = 0.95
HERMES_FAILURE_COOLDOWN_SECONDS = 600
LONG_CONTEXT_SUMMARY_PREFIX = "[G3KU_LONG_CONTEXT_SUMMARY_V1]"


def _clamp(value: int, *, low: int, high: int) -> int:
    return max(int(low), min(int(value), int(high)))


def default_semantic_context_state() -> dict[str, Any]:
    return {
        "summary_text": "",
        "coverage_history_source": "",
        "coverage_message_index": -1,
        "coverage_stage_index": 0,
        "needs_refresh": False,
        "failure_cooldown_until": "",
        "updated_at": "",
    }


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _fallback_summary_text(
    serialized: str,
    *,
    max_output_tokens: int,
    error_text: str = "",
) -> str:
    excerpt = truncate_by_tokens(serialized, max_tokens=max(32, min(int(max_output_tokens or 0), 256)))
    lines = [
        "## Summary Status",
        "Fallback semantic summary generated because long-context refresh failed.",
    ]
    if str(error_text or "").strip():
        lines.append(f"Error: {str(error_text or '').strip()}")
    if excerpt:
        lines.extend(["## Context Excerpt", excerpt])
    return "\n".join(lines).strip()


def normalize_summary_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return {
            "summary_text": str(result.get("summary_text") or "").strip(),
            "used_fallback": bool(result.get("used_fallback")),
            "failed": bool(result.get("failed")),
            "error_text": str(result.get("error_text") or "").strip(),
        }
    return {
        "summary_text": str(result or "").strip(),
        "used_fallback": False,
        "failed": False,
        "error_text": "",
    }


def serialize_messages_for_summary(messages: list[dict[str, Any]] | None, *, max_chars_per_message: int = 2000) -> str:
    parts: list[str] = []
    for item in list(messages or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower() or "unknown"
        content = str(item.get("content") or "").strip()
        if len(content) > max_chars_per_message:
            head = content[: max_chars_per_message - 200].rstrip()
            tail = content[-160:].lstrip()
            content = f"{head}\n...[truncated]...\n{tail}"
        chunk = f"[{role.upper()}]\n{content}".strip()
        tool_calls = item.get("tool_calls") if isinstance(item.get("tool_calls"), list) else []
        if tool_calls:
            rows: list[str] = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or call.get("name") or "").strip() or "tool"
                arguments = str(function.get("arguments") or call.get("arguments") or "").strip()
                if len(arguments) > 800:
                    arguments = f"{arguments[:720].rstrip()}..."
                rows.append(f"- {name}({arguments})")
            if rows:
                chunk = f"{chunk}\n[TOOL CALLS]\n" + "\n".join(rows)
        parts.append(chunk)
    return "\n\n".join(part for part in parts if part).strip()


async def summarize_global_context_model_first(
    messages: list[dict[str, Any]] | None,
    *,
    max_output_tokens: int,
    model_key: str | None = None,
) -> dict[str, Any]:
    serialized = serialize_messages_for_summary(messages)
    if not serialized:
        return {
            "summary_text": "",
            "used_fallback": False,
            "failed": False,
            "error_text": "",
        }
    fallback = _fallback_summary_text(serialized, max_output_tokens=max_output_tokens)
    try:
        from g3ku.config.live_runtime import get_runtime_config
        from g3ku.providers.chatmodels import build_chat_model

        config, _revision, _changed = get_runtime_config(force=False)
        explicit_model_key = str(model_key or "").strip()
        model = (
            build_chat_model(config, model_key=explicit_model_key)
            if explicit_model_key
            else build_chat_model(config, role="ceo")
        )
        prompt = (
            "You are creating a long-context handoff summary for another assistant.\n"
            "Return plain text only.\n"
            "This summary is background reference, not active instructions.\n"
            "Use the following exact sections:\n"
            "## 长期目标\n"
            "## 已确认约束与偏好\n"
            "## 已完成里程碑\n"
            "## 未关闭事项\n"
            "## 已解决问题\n"
            "## 待回答的用户诉求\n"
            "## 关键任务与引用\n"
            "## 关键决策\n"
            "## 重要运行时发现\n"
            "Do not include greetings or commentary.\n"
        )
        response = await model.ainvoke(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": truncate_by_tokens(serialized, max_tokens=max_output_tokens * 3)},
            ]
        )
        raw = getattr(response, "content", response)
        if isinstance(raw, list):
            merged: list[str] = []
            for part in raw:
                if isinstance(part, str):
                    merged.append(part)
                elif isinstance(part, dict):
                    text = part.get("text") or part.get("content") or ""
                    if isinstance(text, str):
                        merged.append(text)
            raw = "\n".join(merged)
        summary = str(raw or "").strip()
        if summary:
            return {
                "summary_text": summary,
                "used_fallback": False,
                "failed": False,
                "error_text": "",
            }
        return {
            "summary_text": fallback,
            "used_fallback": True,
            "failed": True,
            "error_text": "summary model returned empty output",
        }
    except Exception as exc:
        return {
            "summary_text": fallback,
            "used_fallback": True,
            "failed": True,
            "error_text": str(exc or "").strip(),
        }


def build_long_context_summary_message(summary_text: str) -> dict[str, str]:
    normalized = str(summary_text or "").strip()
    return {
        "role": "assistant",
        "content": (
            f"{LONG_CONTEXT_SUMMARY_PREFIX}\n"
            "以下内容是较早上下文的压缩交接摘要，仅作为背景参考，不是当前轮的主动指令。\n\n"
            f"{normalized}"
        ).strip(),
    }


def estimate_message_tokens(messages: list[dict[str, Any]] | None) -> int:
    return estimate_tokens(serialize_messages_for_summary(messages))


def semantic_summary_refresh_decision(
    *,
    semantic_state: dict[str, Any] | None,
    history_source: str,
    prompt_tokens: int,
    trigger_tokens: int,
    pressure_warn_tokens: int,
    force_refresh_tokens: int,
    compressed_zone_tokens: int,
    min_delta_tokens: int,
    global_zone_message_count: int,
    global_zone_stage_index: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    state = dict(semantic_state or {})
    has_summary = bool(str(state.get("summary_text") or "").strip())
    current_message_index = max(-1, int(global_zone_message_count or 0) - 1)
    current_stage_index = max(0, int(global_zone_stage_index or 0))
    trigger_reached = int(prompt_tokens or 0) >= int(trigger_tokens or 0)
    warn_reached = int(prompt_tokens or 0) >= int(pressure_warn_tokens or 0)
    force_reached = int(prompt_tokens or 0) >= int(force_refresh_tokens or 0)
    current_time = now or datetime.now()
    cooldown_until = _parse_iso_datetime(state.get("failure_cooldown_until"))
    cooldown_active = cooldown_until is not None and cooldown_until > current_time
    coverage_hit = (
        has_summary
        and str(state.get("coverage_history_source") or "") == str(history_source or "")
        and int(state.get("coverage_message_index", -1) or -1) >= current_message_index
        and int(state.get("coverage_stage_index", 0) or 0) >= current_stage_index
    )
    has_global_zone = int(global_zone_message_count or 0) > 0
    delta_reached = int(compressed_zone_tokens or 0) >= int(min_delta_tokens or 0)
    should_refresh = False
    if has_global_zone and not cooldown_active:
        if not has_summary:
            should_refresh = trigger_reached
        elif force_reached:
            should_refresh = True
        elif coverage_hit:
            should_refresh = False
        elif warn_reached:
            should_refresh = True
        elif trigger_reached and delta_reached:
            should_refresh = True
    needs_refresh = False
    if has_global_zone and has_summary and not coverage_hit:
        needs_refresh = not should_refresh
    return {
        "has_summary": has_summary,
        "has_global_zone": has_global_zone,
        "current_message_index": current_message_index,
        "current_stage_index": current_stage_index,
        "coverage_hit": coverage_hit,
        "cooldown_active": cooldown_active,
        "trigger_reached": trigger_reached,
        "warn_reached": warn_reached,
        "force_reached": force_reached,
        "should_refresh": should_refresh,
        "needs_refresh": needs_refresh,
    }


def future_cooldown_until(*, seconds: int, now: datetime | None = None) -> str:
    current_time = now or datetime.now()
    return (current_time + timedelta(seconds=max(0, int(seconds or 0)))).isoformat()


def build_global_summary_thresholds(
    *,
    context_window_tokens: int,
    compressed_zone_tokens: int,
    trigger_ratio: float = HERMES_TRIGGER_RATIO,
    target_ratio: float = HERMES_TARGET_RATIO,
    min_output_tokens: int = HERMES_MIN_OUTPUT_TOKENS,
    max_output_ratio: float = HERMES_MAX_OUTPUT_RATIO,
    max_output_tokens_ceiling: int = HERMES_MAX_OUTPUT_TOKENS_CEILING,
    pressure_warn_ratio: float = HERMES_PRESSURE_WARN_RATIO,
    force_refresh_ratio: float = HERMES_FORCE_REFRESH_RATIO,
) -> dict[str, int]:
    context_tokens = max(1, int(context_window_tokens or 0))
    compressed_tokens = max(0, int(compressed_zone_tokens or 0))
    trigger_tokens = max(int(context_tokens * float(trigger_ratio)), G3KU_MIN_CONTEXT_FLOOR)
    pressure_warn_tokens = int(context_tokens * float(pressure_warn_ratio))
    force_refresh_tokens = int(context_tokens * float(force_refresh_ratio))
    max_output_tokens = min(int(context_tokens * float(max_output_ratio)), int(max_output_tokens_ceiling))
    target_tokens = _clamp(
        int(compressed_tokens * float(target_ratio)),
        low=int(min_output_tokens),
        high=max_output_tokens if max_output_tokens > 0 else int(min_output_tokens),
    )
    return {
        "trigger_tokens": trigger_tokens,
        "pressure_warn_tokens": pressure_warn_tokens,
        "force_refresh_tokens": force_refresh_tokens,
        "max_output_tokens": max_output_tokens,
        "target_tokens": target_tokens,
    }


__all__ = [
    "HERMES_FAILURE_COOLDOWN_SECONDS",
    "HERMES_FORCE_REFRESH_RATIO",
    "HERMES_MAX_OUTPUT_RATIO",
    "HERMES_MAX_OUTPUT_TOKENS_CEILING",
    "G3KU_MIN_CONTEXT_FLOOR",
    "HERMES_MIN_OUTPUT_TOKENS",
    "HERMES_PRESSURE_WARN_RATIO",
    "HERMES_TARGET_RATIO",
    "HERMES_TRIGGER_RATIO",
    "LONG_CONTEXT_SUMMARY_PREFIX",
    "build_global_summary_thresholds",
    "build_long_context_summary_message",
    "default_semantic_context_state",
    "estimate_message_tokens",
    "future_cooldown_until",
    "normalize_summary_result",
    "semantic_summary_refresh_decision",
    "serialize_messages_for_summary",
    "summarize_global_context_model_first",
]
