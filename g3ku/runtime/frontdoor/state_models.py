from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class CeoRuntimeContext:
    loop: Any
    session: Any
    session_key: str
    on_progress: Callable[..., Any] | None


@dataclass(slots=True)
class CeoPendingInterrupt:
    interrupt_id: str
    value: Any


class CeoFrontdoorInterrupted(RuntimeError):
    def __init__(self, *, interrupts: list[CeoPendingInterrupt], values: dict[str, Any]) -> None:
        super().__init__("ceo_frontdoor_interrupted")
        self.interrupts = list(interrupts or [])
        self.values = dict(values or {})


def initial_persistent_state(*, user_input: Any) -> dict[str, Any]:
    return {
        "user_input": user_input,
        "approval_request": None,
        "approval_status": "",
    }


__all__ = [
    "CeoFrontdoorInterrupted",
    "CeoPendingInterrupt",
    "CeoRuntimeContext",
    "initial_persistent_state",
]
