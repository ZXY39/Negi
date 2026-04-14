from __future__ import annotations

from typing import Any

MAIN_ACTOR_ROLE = 'ceo'
PUBLIC_ACTOR_ROLES = ('ceo', 'execution', 'inspection')


def to_public_actor_role(actor_role: str | None) -> str:
    value = str(actor_role or '').strip().lower()
    if not value:
        return 'execution'
    if value in PUBLIC_ACTOR_ROLES:
        return value
    if value in {'checker', 'acceptance'}:
        return 'inspection'
    raise ValueError(f'Unsupported actor role: {actor_role}')


def normalize_public_allowed_roles(roles: list[str] | None) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for role in roles or []:
        public_role = to_public_actor_role(role)
        if public_role in seen:
            continue
        seen.add(public_role)
        normalized.append(public_role)
    return normalized


def to_public_allowed_roles(roles: list[str] | None) -> list[str]:
    normalized = normalize_public_allowed_roles(roles)
    return normalized or ['ceo', 'execution']


def to_public_model_defaults(defaults: dict[str, Any] | None) -> dict[str, str]:
    payload = defaults if isinstance(defaults, dict) else {}
    return {
        'ceo': str(payload.get('ceo') or '').strip(),
        'execution': str(payload.get('execution') or '').strip(),
        'inspection': str(payload.get('inspection') or '').strip(),
    }
