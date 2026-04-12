from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


def _field(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _ordered_unique(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in list(values or []):
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(sorted(ordered))


def _visible_skill_ids(visible_skills: list[Any]) -> tuple[str, ...]:
    return _ordered_unique([str(_field(item, "skill_id") or "").strip() for item in list(visible_skills or [])])


def _visible_tool_ids(
    visible_families: list[Any],
    visible_tool_names: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    del visible_families
    return _ordered_unique(visible_tool_names)


def _snapshot_payload(
    *,
    visible_skills: list[Any],
    visible_families: list[Any],
    visible_tool_names: list[str] | tuple[str, ...] | None,
) -> dict[str, list[str]]:
    return {
        "visible_skill_ids": list(_visible_skill_ids(visible_skills)),
        "visible_tool_ids": list(_visible_tool_ids(visible_families, visible_tool_names)),
        "visible_tool_names": list(_ordered_unique(visible_tool_names)),
    }


def _exposure_revision(payload: dict[str, list[str]]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"exp:{hashlib.sha256(encoded).hexdigest()[:16]}"


def _render_id_section(*, heading: str, intro: str, ids: list[str]) -> list[str]:
    lines = [heading, intro]
    if not ids:
        lines.append("- None.")
        return lines
    lines.extend(f"- `{item}`" for item in ids)
    return lines


def _stable_catalog_message(*, payload: dict[str, list[str]], exposure_revision: str) -> str:
    sections = [
        "## Capability Exposure Snapshot",
        f"- Exposure revision: `{exposure_revision}`",
        "- This snapshot is derived from CEO-visible capability exposure only; per-turn ranking must not mutate it.",
        "",
        *_render_id_section(
            heading="## Visible Callable Tools",
            intro="- The callable tool names below are visible in this session.",
            ids=list(payload.get("visible_tool_names") or []),
        ),
        "",
        *_render_id_section(
            heading="## Visible Skills",
            intro='- Call `load_skill_context` only with these visible skill ids.',
            ids=list(payload.get("visible_skill_ids") or []),
        ),
        "",
        *_render_id_section(
            heading="## Visible Tool Context Ids",
            intro='- Call `load_tool_context` only with these visible concrete tool ids.',
            ids=list(payload.get("visible_tool_ids") or []),
        ),
    ]
    return "\n".join(sections).strip()


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    exposure_revision: str
    stable_catalog_message: str
    visible_skill_ids: tuple[str, ...]
    visible_tool_ids: tuple[str, ...]


def build_capability_snapshot(
    *,
    visible_skills: list[Any],
    visible_families: list[Any],
    visible_tool_names: list[str] | tuple[str, ...] | None = None,
) -> CapabilitySnapshot:
    payload = _snapshot_payload(
        visible_skills=list(visible_skills or []),
        visible_families=list(visible_families or []),
        visible_tool_names=visible_tool_names,
    )
    exposure_revision = _exposure_revision(payload)
    return CapabilitySnapshot(
        exposure_revision=exposure_revision,
        stable_catalog_message=_stable_catalog_message(payload=payload, exposure_revision=exposure_revision),
        visible_skill_ids=tuple(payload["visible_skill_ids"]),
        visible_tool_ids=tuple(payload["visible_tool_ids"]),
    )


__all__ = ["CapabilitySnapshot", "build_capability_snapshot"]
