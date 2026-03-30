from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "ContextAssemblyService",
    "ContextAssemblyResult",
    "ContextBlockKind",
    "ContextBlock",
    "ContextSnapshot",
    "ArchiveContextRecord",
    "StageContextRecord",
    "TaskContinuityRecord",
    "RetrievedContextBundle",
]


def __getattr__(name: str) -> Any:
    if name == "ContextAssemblyService":
        return getattr(import_module("g3ku.runtime.context.assembly"), name)
    if name in {
        "ContextAssemblyResult",
        "ContextBlockKind",
        "ContextBlock",
        "ContextSnapshot",
        "ArchiveContextRecord",
        "StageContextRecord",
        "TaskContinuityRecord",
        "RetrievedContextBundle",
    }:
        return getattr(import_module("g3ku.runtime.context.types"), name)
    raise AttributeError(name)
