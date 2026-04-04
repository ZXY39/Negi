from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class StructuredError:
    code: str
    message: str
    recoverable: bool = True
    source: str = "runtime"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_cost_usd_micros: int = 0


@dataclass(slots=True)
class AgentState:
    session_key: str
    system_prompt: str = ""
    model: str = ""
    reasoning_effort: str | None = None
    messages: list[Any] = field(default_factory=list)
    is_running: bool = False
    paused: bool = False
    manual_pause_waiting_reason: bool = False
    status: str = "idle"
    latest_message: str = ""
    stream_message: Any | None = None
    pending_tool_calls: set[str] = field(default_factory=set)
    pending_interrupts: list[dict[str, Any]] = field(default_factory=list)
    queued_steering_messages: list[Any] = field(default_factory=list)
    queued_follow_up_messages: list[Any] = field(default_factory=list)
    last_error: StructuredError | None = None
    event_count: int = 0
    usage_totals: UsageTotals = field(default_factory=UsageTotals)
