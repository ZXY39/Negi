from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class CeoRuntimeContext:
    loop: Any
    session: Any
    session_key: str
    on_progress: Callable[..., Any] | None


def initial_persistent_state(*, user_input: Any) -> dict[str, Any]:
    return {"user_input": user_input}


__all__ = ["CeoRuntimeContext", "initial_persistent_state"]
