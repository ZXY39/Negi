"""Normalization for proxy environment variables consumed by HTTP clients."""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping

# Bracketed IPv6 literal, optionally followed by a subnet prefix or a port.
# httpx/httpx2 treat the bracketed form as a domain and build the proxy-bypass
# pattern "all://*[::1]", which makes client construction fail with
# InvalidURL: Invalid port: ':1]'.
_BRACKETED_IPV6_ENTRY = re.compile(
    r"^\[(?P<address>[0-9A-Fa-f:.]+)\](?::(?P<port>\d+))?(?P<subnet>/\d+)?$"
)


def normalize_no_proxy_env(environ: MutableMapping[str, str] | None = None) -> bool:
    """Rewrite bracketed IPv6 NO_PROXY entries to the bare literal.

    ``NO_PROXY=[::1]`` is valid for most HTTP clients, but the proxy-bypass
    parsing in httpx/httpx2 classifies it as a domain and aborts client
    construction. Proxy-bypass patterns carry host names only, so the port
    component is dropped. Returns True when an environment value changed.
    """
    env = os.environ if environ is None else environ
    changed = False
    for name in ("NO_PROXY", "no_proxy"):
        raw = env.get(name)
        if not raw:
            continue
        entries = raw.split(",")
        rewritten = False
        for index, entry in enumerate(entries):
            match = _BRACKETED_IPV6_ENTRY.match(entry.strip())
            if match is None:
                continue
            entries[index] = match.group("address") + (match.group("subnet") or "")
            rewritten = True
        if not rewritten:
            continue
        normalized = ",".join(entries)
        if normalized != raw:
            env[name] = normalized
            changed = True
    return changed
