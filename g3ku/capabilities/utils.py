from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from g3ku import __version__

CAPABILITY_API_VERSION = "1.0"


def import_string(path: str) -> Any:
    module_name, _, attr = str(path or "").partition(":")
    if not module_name or not attr:
        raise ValueError(f"Invalid entrypoint: {path!r}")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def callable_accepts_keyword(fn: Callable[..., Any], keyword: str) -> bool:
    try:
        return keyword in fn.__code__.co_varnames
    except Exception:
        return False


def is_compatible(spec: str | None, value: str) -> tuple[bool, str | None]:
    raw = str(spec or "").strip()
    if not raw:
        return True, None
    normalized = raw.replace("x", "*")
    if normalized.endswith(".*"):
        prefix = normalized[:-2]
        return value.startswith(prefix), None if value.startswith(prefix) else f"{value} not in {raw}"
    try:
        return Version(value) in SpecifierSet(normalized), None
    except (InvalidSpecifier, InvalidVersion):
        return value == raw, None if value == raw else f"{value} != {raw}"


def check_compat(compat: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    core_spec = str(compat.get("g3ku_core") or "")
    core_ok, core_msg = is_compatible(core_spec, __version__)
    if not core_ok:
        errors.append(f"g3ku_core compatibility failed: {core_msg or f'{__version__} not in {core_spec}'}")
    api_spec = str(compat.get("capability_api") or "")
    api_ok, api_msg = is_compatible(api_spec, CAPABILITY_API_VERSION)
    if not api_ok:
        errors.append(f"capability_api compatibility failed: {api_msg or f'{CAPABILITY_API_VERSION} not in {api_spec}'}")
    return errors, warnings


def check_requires(requires: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for bin_name in requires.get("bins", []) or []:
        if not shutil.which(str(bin_name)):
            errors.append(f"missing CLI dependency: {bin_name}")
    for env_name in requires.get("env", []) or []:
        if not os.environ.get(str(env_name)):
            errors.append(f"missing environment variable: {env_name}")
    for mod_name in requires.get("python", []) or []:
        try:
            importlib.import_module(str(mod_name))
        except Exception:
            errors.append(f"missing Python dependency: {mod_name}")
    return errors, warnings


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_file():
        h.update(path.read_bytes())
        return h.hexdigest()
    for item in sorted(path.rglob('*')):
        if item.is_file():
            h.update(str(item.relative_to(path)).encode('utf-8', errors='ignore'))
            h.update(item.read_bytes())
    return h.hexdigest()


def import_from_file(file_path: Path, attribute: str) -> Any:
    module_name = f"g3ku_dynamic_{file_path.stem}_{abs(hash(str(file_path)))}"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, attribute)

