from __future__ import annotations

from ._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from ._ceo_langgraph_impl import CeoFrontDoorRunner as LegacyCeoFrontDoorRunner


def _create_agent_enabled(loop) -> bool:
    assembly = getattr(getattr(loop, "_memory_runtime_settings", None), "assembly", None)
    return bool(getattr(assembly, "frontdoor_create_agent_enabled", False))


class CeoFrontDoorRunner:
    def __init__(self, *, loop) -> None:
        impl_cls = CreateAgentCeoFrontDoorRunner if _create_agent_enabled(loop) else LegacyCeoFrontDoorRunner
        self._impl = impl_cls(loop=loop)

    def __getattr__(self, name: str):
        return getattr(self._impl, name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_impl" or "_impl" not in self.__dict__:
            object.__setattr__(self, name, value)
            return
        setattr(self._impl, name, value)


__all__ = ["CeoFrontDoorRunner", "CreateAgentCeoFrontDoorRunner", "LegacyCeoFrontDoorRunner"]
