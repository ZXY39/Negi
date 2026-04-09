from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from g3ku.runtime.frontdoor.capability_snapshot import build_capability_snapshot


REWRITE_PROMPT_REVISION = "frontdoor-query-rewrite:v1"


def _normalized_text(value: Any) -> str:
    return str(value or "").strip()


def _stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonicalize_visible_ids(values: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in list(values or []):
        normalized = _normalized_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return sorted(ordered)


def build_query_rewrite_runtime_identity(*, model_key: str, runtime_revision: Any) -> str:
    digest = _stable_digest(
        {
            "model_key": _normalized_text(model_key),
            "runtime_revision": _normalized_text(runtime_revision),
        }
    )
    return f"frontdoor:catalog_query_rewrite_runtime:{digest}"


def build_query_rewrite_cache_key(
    raw_query: str,
    exposure_revision: str,
    rewrite_prompt_revision: str,
) -> str:
    digest = _stable_digest(
        {
            "raw_query": _normalized_text(raw_query),
            "exposure_revision": _normalized_text(exposure_revision),
            "rewrite_prompt_revision": _normalized_text(rewrite_prompt_revision),
        }
    )
    return f"frontdoor:catalog_query_rewrite:{digest}"


def build_query_rewrite_exposure_revision(
    *,
    visible_skill_ids: list[str],
    visible_tool_ids: list[str],
) -> str:
    snapshot = build_capability_snapshot(
        visible_skills=[{"skill_id": skill_id} for skill_id in canonicalize_visible_ids(visible_skill_ids)],
        visible_families=[{"tool_id": tool_id} for tool_id in canonicalize_visible_ids(visible_tool_ids)],
        visible_tool_names=(),
    )
    return _normalized_text(snapshot.exposure_revision)


@dataclass(frozen=True)
class FrontdoorRewriteResult:
    raw_query: str
    skill_query: str
    tool_query: str
    status: str
    model: str
    exposure_revision: str
    cache_key: str
    rewrite_prompt_revision: str = REWRITE_PROMPT_REVISION
