from __future__ import annotations

from ._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner


class CeoFrontDoorRunner:
    def __init__(self, *, loop) -> None:
        self._impl = CreateAgentCeoFrontDoorRunner(loop=loop)

    def invalidate_runtime_bindings(self) -> None:
        setattr(self._impl, "_agent", None)
        setattr(self._impl, "_compiled_graph", None)

    def __getattr__(self, name: str):
        return getattr(self._impl, name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_impl" or "_impl" not in self.__dict__:
            object.__setattr__(self, name, value)
            return
        setattr(self._impl, name, value)


__all__ = ["CeoFrontDoorRunner", "CreateAgentCeoFrontDoorRunner"]
