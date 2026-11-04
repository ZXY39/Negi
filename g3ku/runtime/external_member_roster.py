"""Persistent session-local member numbers for channel mentions.

Transcript-derived numbers only know senders whose messages became turns.
Background digest senders never do, so they need a durable roster. The roster
is the source of truth for ``mention_member`` names/numbers and for the numbers
shown in digest artifacts; it never stores raw message text.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Iterable

from g3ku.deployment.data_root import data_work_path


def _safe_session(session_key: str) -> str:
    import hashlib
    import re

    raw = str(session_key or "").strip()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)[:60].strip("._") or "session"
    return f"{label}-{digest}"


class MemberRoster:
    def __init__(self, session_key: str) -> None:
        self.session_key = str(session_key or "").strip()
        self.path = data_work_path(
            "external-sessions", "members", f"{_safe_session(self.session_key)}.json"
        )
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        members = payload.get("members")
        if not isinstance(members, dict):
            members = {}
        try:
            next_number = max(2, int(payload.get("next_number") or 2))
        except (TypeError, ValueError):
            next_number = 2
        return {"next_number": next_number, "members": members}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def update_many(self, entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Ensure each sender has a stable number; returns the full member map."""
        with self._lock:
            payload = self._read()
            members: dict[str, dict[str, Any]] = payload["members"]
            next_number = int(payload["next_number"])
            changed = False
            for entry in entries or []:
                sender_id = str(entry.get("id") or entry.get("sender_id") or "").strip()
                if not sender_id:
                    continue
                current = members.get(sender_id)
                if not isinstance(current, dict):
                    current = {}
                name = str(entry.get("name") or entry.get("sender_name") or "").strip()
                role = str(entry.get("role") or "").strip()
                master = bool(entry.get("master") or entry.get("is_master"))
                update: dict[str, Any] = {}
                if name and current.get("name") != name:
                    update["name"] = name
                if role and current.get("role") != role:
                    update["role"] = role
                if bool(current.get("master")) != master:
                    update["master"] = master
                number = current.get("number")
                if master:
                    number = 0
                elif not isinstance(number, int) or number < 2:
                    number = next_number
                    next_number += 1
                if current.get("number") != number:
                    update["number"] = number
                if update:
                    members[sender_id] = {**current, **update}
                    changed = True
                elif sender_id not in members:
                    members[sender_id] = {**current, "number": number}
                    changed = True
            if changed:
                payload["next_number"] = next_number
                self._write(payload)
            return members

    def members(self) -> dict[str, dict[str, Any]]:
        return dict(self._read()["members"])

    def resolve(self, target: str) -> dict[str, Any]:
        """Resolve Master / number / unique nickname to a member entry.

        Returns ``{"ok": True, "id", "number", "name", "role"}`` or an error
        payload with candidates so the model can retry with a number.
        """
        raw = str(target or "").strip()
        members = self.members()
        if not raw:
            return {"ok": False, "reason": "target_required"}
        if raw.lower() == "master":
            for sender_id, item in members.items():
                if bool(item.get("master")):
                    return {"ok": True, "id": sender_id, **item}
            return {"ok": False, "reason": "master_not_in_roster"}
        number_match = re.fullmatch(r"(?:第三方\s*)?#?(\d{1,6})", raw)
        if number_match:
            wanted = int(number_match.group(1))
            if wanted == 1:
                for sender_id, item in members.items():
                    if bool(item.get("master")):
                        return {"ok": True, "id": sender_id, **item, "number": 1}
                return {"ok": False, "reason": "master_not_in_roster"}
            for sender_id, item in members.items():
                if int(item.get("number") or 0) == wanted:
                    return {"ok": True, "id": sender_id, **item}
            return {
                "ok": False,
                "reason": "target_not_found",
                "known_numbers": sorted(
                    f"{int(item.get('number')):04d}"
                    for item in members.values()
                    if isinstance(item.get("number"), int) and int(item.get("number")) >= 2
                ),
            }
        matches = [
            (sender_id, item)
            for sender_id, item in members.items()
            if str(item.get("name") or "").strip() == raw
        ]
        if len(matches) == 1:
            sender_id, item = matches[0]
            return {"ok": True, "id": sender_id, **item}
        if len(matches) > 1:
            return {
                "ok": False,
                "reason": "ambiguous_name",
                "candidates": [
                    {
                        "number": int(item.get("number") or 0),
                        "name": str(item.get("name") or ""),
                        "role": str(item.get("role") or ""),
                    }
                    for _, item in matches
                ],
                "message": "This nickname matches multiple members; retry with the member number.",
            }
        return {
            "ok": False,
            "reason": "target_not_found",
            "message": "Unknown nickname; retry with a member number.",
            "known_members": [
                {
                    "number": int(item.get("number") or 0),
                    "name": str(item.get("name") or ""),
                    "role": str(item.get("role") or ""),
                }
                for item in members.values()
                if isinstance(item.get("number"), int) and int(item.get("number")) >= 2
            ],
        }


def get_member_roster(session_key: str) -> MemberRoster:
    return MemberRoster(session_key)


__all__ = ["MemberRoster", "get_member_roster"]
