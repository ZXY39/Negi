from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["CeoFrontDoorRunner", "CeoExposureResolver", "CeoPromptBuilder"]


def __getattr__(name: str) -> Any:
    if name == "CeoFrontDoorRunner":
        return getattr(import_module("g3ku.runtime.frontdoor.ceo_runner"), name)
    if name == "CeoExposureResolver":
        return getattr(import_module("g3ku.runtime.frontdoor.exposure_resolver"), name)
    if name == "CeoPromptBuilder":
        return getattr(import_module("g3ku.runtime.frontdoor.prompt_builder"), name)
    raise AttributeError(name)
