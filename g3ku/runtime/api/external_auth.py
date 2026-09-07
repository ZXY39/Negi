"""Bearer-token authentication dependency for the External Agent API.

Each bridge application is provisioned one token entry under
``externalApi.tokens.<bridge_id>`` (``g3ku/config/schema.py``). Token secrets
are stored in the bootstrap secret overlay (extracted on save, stripped from
the on-disk payload, re-applied on unlock), so the in-memory config exposed
here already carries resolved values. Project-level locking is enforced
upstream by the global bootstrap lock middleware (423 for every ``/api/*``
route while the project is locked); this dependency only covers the
API-enable gate and the per-bridge credential check.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException

from g3ku.config.live_runtime import get_runtime_config


@dataclass(slots=True)
class ExternalApiPrincipal:
    bridge_id: str
    label: str


def _extract_bearer_token(authorization: str | None) -> str | None:
    raw = str(authorization or "").strip()
    if not raw.lower().startswith("bearer "):
        return None
    token = raw[7:].strip()
    return token or None


def require_external_api(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ExternalApiPrincipal:
    config = get_runtime_config(force=False)[0]
    external_api = getattr(config, "external_api", None)
    if external_api is None or not getattr(external_api, "enabled", False):
        raise HTTPException(status_code=403, detail="external_api_disabled")

    presented = _extract_bearer_token(authorization)
    if not presented:
        raise HTTPException(status_code=401, detail="invalid_api_token")

    for bridge_id, entry in dict(getattr(external_api, "tokens", None) or {}).items():
        if not getattr(entry, "enabled", True):
            continue
        expected = str(getattr(entry, "token", "") or "")
        if not expected:
            continue
        if secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
            return ExternalApiPrincipal(
                bridge_id=str(bridge_id),
                label=str(getattr(entry, "label", "") or ""),
            )
    raise HTTPException(status_code=401, detail="invalid_api_token")
