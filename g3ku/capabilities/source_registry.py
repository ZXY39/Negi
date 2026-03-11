from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass(slots=True)
class CapabilitySourcePolicy:
    allow_builtin: bool = True
    allow_local: bool = True
    allow_git: bool = True
    allowed_git_hosts: list[str] = field(default_factory=list)


class CapabilitySourceRegistry:
    """Describe and validate allowed capability source types."""

    def __init__(self, policy: CapabilitySourcePolicy | None = None):
        self.policy = policy or CapabilitySourcePolicy()

    def describe_sources(self) -> list[dict[str, Any]]:
        hosts = [host.lower() for host in self.policy.allowed_git_hosts]
        return [
            {
                "type": "builtin",
                "enabled": bool(self.policy.allow_builtin),
                "notes": ["always local to the g3ku installation"],
            },
            {
                "type": "local",
                "enabled": bool(self.policy.allow_local),
                "notes": ["workspace/local filesystem capability pack path"],
            },
            {
                "type": "git",
                "enabled": bool(self.policy.allow_git),
                "notes": [
                    "git repository install/update source",
                    *( [f"allowed hosts: {', '.join(hosts)}"] if hosts else [] ),
                ],
            },
        ]

    def validate_request(self, *, source_type: str, source_uri: str | None = None) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        normalized = str(source_type or "").strip().lower()
        if normalized == "builtin":
            if not self.policy.allow_builtin:
                errors.append("builtin capability operations are disabled by policy")
            return errors, warnings
        if normalized == "local":
            if not self.policy.allow_local:
                errors.append("local capability installs are disabled by policy")
            return errors, warnings
        if normalized == "git":
            if not self.policy.allow_git:
                errors.append("git capability installs are disabled by policy")
                return errors, warnings
            hosts = [host.lower() for host in self.policy.allowed_git_hosts if str(host).strip()]
            if hosts:
                host = self._extract_git_host(source_uri or "")
                if not host:
                    errors.append("could not determine git host for policy check")
                elif host.lower() not in hosts:
                    errors.append(f"git host '{host}' is not in allowed_git_hosts")
            return errors, warnings
        errors.append(f"unsupported capability source type: {source_type}")
        return errors, warnings

    @staticmethod
    def _extract_git_host(source_uri: str) -> str | None:
        raw = str(source_uri or "").strip()
        if not raw:
            return None
        parsed = urlparse(raw)
        if parsed.hostname:
            return parsed.hostname
        scp_like = re.match(r"^(?:.+@)?([^:]+):.+$", raw)
        if scp_like:
            return scp_like.group(1)
        return None
