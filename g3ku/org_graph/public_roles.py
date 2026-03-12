from __future__ import annotations

from typing import Any


PUBLIC_ACTOR_ROLES = ("ceo", "execution", "inspection")


def to_public_actor_role(actor_role: str | None) -> str:
    value = str(actor_role or "").strip().lower()
    if value == "ceo":
        return "ceo"
    if value == "execution":
        return "execution"
    if value in {"checker", "inspection"}:
        return "inspection"
    return "execution"


def to_public_allowed_roles(roles: list[str] | None) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for role in roles or []:
        public_role = to_public_actor_role(role)
        if public_role in seen:
            continue
        seen.add(public_role)
        normalized.append(public_role)
    return normalized or ["ceo", "execution"]


def to_public_model_defaults(defaults: dict[str, Any] | None) -> dict[str, str]:
    payload = defaults if isinstance(defaults, dict) else {}
    return {
        "agent": str(payload.get("agent") or "").strip(),
        "ceo": str(payload.get("ceo") or "").strip(),
        "execution": str(payload.get("execution") or "").strip(),
        "inspection": str(payload.get("inspection") or "").strip(),
    }


def public_role_label(actor_role: str | None) -> str:
    role = to_public_actor_role(actor_role)
    return {
        "ceo": "CEO",
        "execution": "执行",
        "inspection": "检验",
    }[role]
