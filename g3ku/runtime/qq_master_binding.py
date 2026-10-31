"""QQ Master one-time binding codes.

The operator generates a short-lived code from the local admin surface after
entering the project unlock password, then sends it to the bot from their own
QQ account in a private chat. The inbound message is intercepted before it
becomes a turn: the sender's ``user_openid`` becomes the account's owner and
the code is consumed.

Two boundaries are deliberate:

* Only DMs can bind. QQ group messages carry ``member_openid``, a different id
  space from the c2c ``user_openid``; binding one would not identify the same
  person in private chat.
* Codes live in memory for five minutes and only while a code is active does
  the inbound path compare text. After registration (or expiry) there is
  nothing to scan.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger

from g3ku.config.loader import load_config, save_config

BINDING_TTL_SECONDS = 300
BINDING_PREFIX = "NEGI-MASTER"
_OWNER_OPENID_RE = re.compile(r"^[0-9a-fA-F]{16,128}$")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")


class QqMasterBindingError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = str(code or "qq_master_binding_error")
        self.message = str(message or "")
        self.status_code = int(status_code)


@dataclass
class _ActiveBinding:
    app_id: str
    token_hash: str
    expires_at_monotonic: float
    issued_at: str


class QqMasterBindingRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[str, _ActiveBinding] = {}

    def issue(self, app_id: str) -> dict[str, Any]:
        normalized = str(app_id or "").strip()
        if not normalized:
            raise QqMasterBindingError("qq_app_id_required", "缺少 QQ 号", status_code=400)
        token = f"{BINDING_PREFIX}-" + "-".join(
            secrets.token_hex(2).upper() for _ in range(4)
        )
        now = time.monotonic()
        with self._lock:
            self._active[normalized] = _ActiveBinding(
                app_id=normalized,
                token_hash=_token_hash(token),
                expires_at_monotonic=now + BINDING_TTL_SECONDS,
                issued_at=datetime.now().isoformat(),
            )
        return {
            "token": token,
            "app_id": normalized,
            "expires_in_seconds": BINDING_TTL_SECONDS,
        }

    def status(self, app_id: str) -> dict[str, Any]:
        normalized = str(app_id or "").strip()
        with self._lock:
            active = self._active.get(normalized)
            if active is None:
                return {"pending": False, "expires_in_seconds": 0}
            remaining = int(active.expires_at_monotonic - time.monotonic())
            if remaining <= 0:
                self._active.pop(normalized, None)
                return {"pending": False, "expires_in_seconds": 0}
            return {"pending": True, "expires_in_seconds": remaining}

    def has_active(self, app_id: str) -> bool:
        return bool(self.status(app_id)["pending"])

    def match(self, app_id: str, text: Any) -> bool:
        """Whether this DM text is the active code for ``app_id``.

        Returns True without consuming; the caller decides whether to bind.
        """
        normalized = str(app_id or "").strip()
        candidate = _token_hash(str(text or ""))
        with self._lock:
            active = self._active.get(normalized)
            if active is None:
                return False
            if active.expires_at_monotonic <= time.monotonic():
                self._active.pop(normalized, None)
                return False
            return bool(candidate) and candidate == active.token_hash

    def consume(self, app_id: str) -> bool:
        normalized = str(app_id or "").strip()
        with self._lock:
            return self._active.pop(normalized, None) is not None

    def invalidate(self, app_id: str) -> None:
        self.consume(app_id)


_REGISTRY = QqMasterBindingRegistry()


def get_qq_master_binding_registry() -> QqMasterBindingRegistry:
    return _REGISTRY


def reset_qq_master_binding_registry() -> None:
    _REGISTRY._active.clear()  # test helper


def _token_hash(token: Any) -> str:
    raw = _normalize_token_text(token)
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_token_text(value: Any) -> str:
    text = _ZERO_WIDTH_RE.sub("", str(value or "")).strip().lower()
    return re.sub(r"\s+", "", text)


def app_id_for_bridge(bridge_id: Any) -> str:
    raw = str(bridge_id or "").strip()
    prefix = "qq-official-"
    return raw[len(prefix):] if raw.startswith(prefix) else ""


def _bridge_id_for_app(app_id: str) -> str:
    from g3ku.qq_official.messages import bridge_id_for_app_id

    return bridge_id_for_app_id(app_id)


def _is_dm_entry(entry: Any) -> bool:
    chat_type = str(getattr(entry, "chat_type", "") or "").strip().lower()
    return chat_type in {"dm", "guild_dm"}


def _dm_peer_id(external_key: Any) -> str:
    parts = str(external_key or "").strip().split(":")
    peer = ""
    if len(parts) == 3 and parts[0] == "qq" and parts[1] == "c2c":
        peer = parts[2]
    elif len(parts) == 4 and parts[0] == "qq" and parts[1] == "guilddm":
        peer = parts[3]
    return peer if _OWNER_OPENID_RE.fullmatch(peer) else ""


def _mask_openid(value: Any) -> str:
    raw = str(value or "").strip()
    return f"{raw[:6]}…{raw[-4:]}" if len(raw) > 10 else raw


def _qq_account(cfg: Any, app_id: str) -> Any:
    accounts = dict(getattr(getattr(cfg, "qq_bot", None), "accounts", None) or {})
    account = accounts.get(app_id)
    if account is None:
        raise QqMasterBindingError(
            "qq_account_not_found", "这个 QQ 号不在配置里", status_code=404
        )
    if getattr(account, "enabled", True) is False:
        raise QqMasterBindingError(
            "qq_account_disabled", "这个 QQ 号已停用，先启用再注册", status_code=409
        )
    return account


def generate_binding(app_id: str) -> dict[str, Any]:
    normalized = str(app_id or "").strip()
    cfg = load_config()
    account = _qq_account(cfg, normalized)
    owners = list(getattr(account, "owner_openids", None) or [])
    if owners:
        raise QqMasterBindingError(
            "master_already_registered", "这个 QQ 号已经注册了 Master", status_code=409
        )
    return get_qq_master_binding_registry().issue(normalized)


def binding_status(app_id: str) -> dict[str, Any]:
    normalized = str(app_id or "").strip()
    cfg = load_config()
    account = _qq_account(cfg, normalized)
    owners = list(getattr(account, "owner_openids", None) or [])
    state = get_qq_master_binding_registry().status(normalized)
    return {
        "app_id": normalized,
        "registered": bool(owners),
        "pending": bool(state["pending"]) and not owners,
        "expires_in_seconds": int(state["expires_in_seconds"]) if not owners else 0,
        "owners": [_mask_openid(item) for item in owners],
    }


async def _refresh_after_save(reason: str) -> None:
    try:
        from g3ku.shells.web import refresh_web_agent_runtime

        await refresh_web_agent_runtime(force=True, reason=reason)
    except Exception as exc:  # noqa: BLE001 - 配置已落盘，刷新失败不丢绑定
        logger.warning("qq master binding runtime refresh failed: {}", exc)


def _upgrade_dm_sessions(bridge_id: str, sender_id: str) -> int:
    from g3ku.runtime.external_sessions import get_external_session_registry

    registry = get_external_session_registry()
    upgraded = 0
    for entry in list(registry.list_bridge_sessions(bridge_id) or []):
        if not _is_dm_entry(entry):
            continue
        peer = _dm_peer_id(getattr(entry, "external_key", ""))
        if not peer or peer.lower() != sender_id.lower():
            continue
        if registry.trust_owner(str(getattr(entry, "session_key", "") or "")) is not None:
            upgraded += 1
    return upgraded


async def complete_binding(
    app_id: str,
    sender_id: str,
    *,
    session_key: str,
) -> dict[str, Any]:
    normalized_app = str(app_id or "").strip()
    normalized_sender = str(sender_id or "").strip()
    if not _OWNER_OPENID_RE.fullmatch(normalized_sender):
        raise QqMasterBindingError(
            "sender_id_not_registerable", "发送者 id 不是可登记的形状", status_code=400
        )
    cfg = load_config()
    account = _qq_account(cfg, normalized_app)
    owners = list(getattr(account, "owner_openids", None) or [])
    already = normalized_sender.lower() in {str(item).lower() for item in owners}
    registered = False
    if not owners:
        owners = [normalized_sender]
        account.owner_openids = owners
        save_config(cfg)
        registered = True
        await _refresh_after_save("qq_master_binding")
    get_qq_master_binding_registry().consume(normalized_app)
    bridge_id = _bridge_id_for_app(normalized_app)
    upgraded = _upgrade_dm_sessions(bridge_id, normalized_sender) if registered else 0
    from g3ku import audit_events

    audit_events.emit_audit_event(
        "external_api",
        "warning",
        "qq_master_registered",
        "QQ Master registered by one-time binding code",
        detail={
            "app_id": normalized_app,
            "session_id": str(session_key or ""),
            "sender_id": _mask_openid(normalized_sender),
            "registered": registered,
            "already_registered": already,
            "dm_sessions_upgraded": upgraded,
            "actor": "operator",
        },
    )
    return {
        "registered": registered,
        "already_registered": already,
        "session_id": str(session_key or ""),
        "dm_sessions_upgraded": upgraded,
    }


async def unregister_master(app_id: str) -> dict[str, Any]:
    normalized = str(app_id or "").strip()
    cfg = load_config()
    account = _qq_account(cfg, normalized)
    owners = list(getattr(account, "owner_openids", None) or [])
    account.owner_openids = []
    save_config(cfg)
    get_qq_master_binding_registry().invalidate(normalized)
    bridge_id = _bridge_id_for_app(normalized)
    from g3ku.runtime.external_sessions import get_external_session_registry

    downgraded = get_external_session_registry().clear_owner_trust(bridge_id)
    await _refresh_after_save("qq_master_unregister")
    from g3ku import audit_events

    audit_events.emit_audit_event(
        "external_api",
        "warning",
        "qq_master_unregistered",
        "QQ Master owner list cleared",
        detail={
            "app_id": normalized,
            "cleared_owner_count": len(owners),
            "dm_sessions_downgraded": downgraded,
            "actor": "operator",
        },
    )
    return {
        "app_id": normalized,
        "cleared_owner_count": len(owners),
        "dm_sessions_downgraded": downgraded,
    }


__all__ = [
    "BINDING_TTL_SECONDS",
    "QqMasterBindingError",
    "QqMasterBindingRegistry",
    "app_id_for_bridge",
    "binding_status",
    "complete_binding",
    "generate_binding",
    "get_qq_master_binding_registry",
    "reset_qq_master_binding_registry",
    "unregister_master",
]
