"""Configuration module for g3ku."""

from __future__ import annotations

__all__ = ["Config", "load_config", "get_config_path"]


def __getattr__(name: str):
    if name == "Config":
        from g3ku.config.schema import Config

        return Config
    if name in {"load_config", "get_config_path"}:
        from g3ku.config.loader import get_config_path, load_config

        return load_config if name == "load_config" else get_config_path
    raise AttributeError(f"module 'g3ku.config' has no attribute {name!r}")
