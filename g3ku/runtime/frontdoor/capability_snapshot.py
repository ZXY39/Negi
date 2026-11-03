from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
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
    visible_tools: list[Any],
    visible_tool_names: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    del visible_tools
    return _ordered_unique(visible_tool_names)


def _snapshot_payload(
    *,
    visible_skills: list[Any],
    visible_tools: list[Any],
    visible_tool_names: list[str] | tuple[str, ...] | None,
) -> dict[str, list[str]]:
    return {
        "visible_skill_ids": list(_visible_skill_ids(visible_skills)),
        "visible_tool_ids": list(_visible_tool_ids(visible_tools, visible_tool_names)),
        "visible_tool_names": list(_ordered_unique(visible_tool_names)),
    }


def _exposure_revision(payload: dict[str, list[str]]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"exp:{hashlib.sha256(encoded).hexdigest()[:16]}"


# 会话级钉住的能力曝光锚：资源增删不改这一串，只有沙箱档位切换才换（压缩也不换）。
# 它进 system 首条、prompt_refresh_stamp 与 prompt_cache_key；实时可见集只服务
# 诊断/准入，不再直接写进可复用前缀。按 session_key 分桶、LRU 封顶，进程表兜底；
# 会话侧车里的 `capability_snapshot_exposure_revision` 是它的耐久载体。
_PINNED_EXPOSURE_STORE_LIMIT = 256
_PINNED_EXPOSURE_STORES: "OrderedDict[str, dict[str, str]]" = OrderedDict()


def _write_pinned_exposure_revision(
    key: str,
    *,
    revision: str,
    sandbox_tier: str,
) -> None:
    normalized_key = str(key or "").strip()
    if not normalized_key:
        return
    _PINNED_EXPOSURE_STORES[normalized_key] = {
        "revision": str(revision or "").strip(),
        "tier": str(sandbox_tier or "").strip(),
    }
    _PINNED_EXPOSURE_STORES.move_to_end(normalized_key)
    while len(_PINNED_EXPOSURE_STORES) > _PINNED_EXPOSURE_STORE_LIMIT:
        _PINNED_EXPOSURE_STORES.popitem(last=False)


def pinned_exposure_revision_for(
    session: Any,
    *,
    session_key: str,
    live_revision: str,
    sandbox_tier: str = "",
    seed_revision: str = "",
) -> str:
    """返回本会话钉住的 exposure revision。

    - 首次（进程表无记录）：用侧车恢复值 / 会话属性 / 实时值播种；
    - 同一档位：始终返回已钉住值，资源增删不改变 system 首条；
    - 档位切换：用当刻实时值更新（用户允许的唯一边界之一）。
    """
    key = str(session_key or "").strip()
    tier = str(sandbox_tier or "").strip()
    live = str(live_revision or "").strip()
    seed = str(seed_revision or "").strip()
    if not seed and session is not None:
        seed = str(
            getattr(session, "_frontdoor_capability_snapshot_exposure_revision", "") or ""
        ).strip()
    if not key:
        return seed or live
    stored = _PINNED_EXPOSURE_STORES.get(key)
    if isinstance(stored, dict):
        stored_revision = str(stored.get("revision") or "").strip()
        stored_tier = str(stored.get("tier") or "").strip()
        if stored_revision and stored_tier == tier:
            return stored_revision
        if live:
            _write_pinned_exposure_revision(key, revision=live, sandbox_tier=tier)
            return live
        return stored_revision or seed or live
    resolved = seed or live
    if resolved:
        _write_pinned_exposure_revision(key, revision=resolved, sandbox_tier=tier)
    return resolved


def drop_pinned_exposure_revision(session_key: Any) -> None:
    _PINNED_EXPOSURE_STORES.pop(str(session_key or "").strip(), None)


def clear_pinned_exposure_revision_stores() -> None:
    """测试隔离：进程表跨用例可见。"""
    _PINNED_EXPOSURE_STORES.clear()


def render_stable_catalog_message(*, exposure_revision: str) -> str:
    """渲染 `## Capability Exposure Snapshot` 那一节。

    调用方传**钉住值**；实时 `CapabilitySnapshot.exposure_revision` 只留在诊断与
    状态里，不进可复用前缀。
    """
    return _stable_catalog_message(exposure_revision=exposure_revision)


def _render_id_section(*, heading: str, intro: str, ids: list[str]) -> list[str]:
    lines = [heading, intro]
    if not ids:
        lines.append("- None.")
        return lines
    lines.extend(f"- `{item}`" for item in ids)
    return lines


def _stable_catalog_message(*, exposure_revision: str) -> str:
    sections = [
        "## Capability Exposure Snapshot",
        f"- Exposure revision: `{exposure_revision}`",
        "- This snapshot is derived from CEO-visible capability exposure only; per-turn ranking must not mutate it.",
        "- It is a stable exposure revision anchor, not the current turn's callable or candidate tool contract.",
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
    visible_tools: list[Any],
    visible_tool_names: list[str] | tuple[str, ...] | None = None,
) -> CapabilitySnapshot:
    payload = _snapshot_payload(
        visible_skills=list(visible_skills or []),
        visible_tools=list(visible_tools or []),
        visible_tool_names=visible_tool_names,
    )
    exposure_revision = _exposure_revision(payload)
    return CapabilitySnapshot(
        exposure_revision=exposure_revision,
        stable_catalog_message=_stable_catalog_message(exposure_revision=exposure_revision),
        visible_skill_ids=tuple(payload["visible_skill_ids"]),
        visible_tool_ids=tuple(payload["visible_tool_ids"]),
    )


__all__ = [
    "CapabilitySnapshot",
    "build_capability_snapshot",
    "clear_pinned_exposure_revision_stores",
    "drop_pinned_exposure_revision",
    "pinned_exposure_revision_for",
    "render_stable_catalog_message",
]
