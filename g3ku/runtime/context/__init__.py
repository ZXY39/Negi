from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "ContextAssemblyResult",
    "RetrievedContextBundle",
]


def __getattr__(name: str) -> Any:
    if name in {"ContextAssemblyResult", "RetrievedContextBundle"}:
        return getattr(import_module("g3ku.runtime.context.types"), name)
    raise AttributeError(name)
