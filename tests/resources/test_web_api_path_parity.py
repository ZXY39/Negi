"""Every literal API path the web frontend calls must be a route the web app serves.

This lane drifted once: the exec-approval and command-allowlist calls carried an extra
`/admin` segment that no router is mounted under, so the panel polled a 404 forever while
both sides of the code looked correct on their own. The judge is the real mounted app, not
a re-listing of prefixes, because a prefix restated in the test is the same second source
of truth that produced the bug.
"""

from __future__ import annotations

import re
from pathlib import Path

from g3ku.web import main as web_main

FRONTEND_DIR = Path(web_main.__file__).resolve().parent / "frontend"

_CALL_RE = re.compile(
    r"""\b(?:this|ApiClient|api)\.(get|post|put|delete|patch)\(\s*(?:"([^"\n]*)"|`([^`\n]*)`)""",
    re.IGNORECASE,
)


def _registered_routes() -> set[tuple[str, tuple[str, ...]]]:
    routes: set[tuple[str, tuple[str, ...]]] = set()
    for route in web_main.app.routes:
        methods = set(getattr(route, "methods", None) or set())
        path = getattr(route, "path", "")
        if not path or not methods:
            continue
        for method in methods:
            if method.upper() in {"HEAD", "OPTIONS"}:
                continue
            routes.add((method.upper(), tuple(seg for seg in path.split("/") if seg)))
    return routes


def _frontend_calls() -> list[tuple[str, tuple[str, ...], str]]:
    calls: list[tuple[str, tuple[str, ...], str]] = []
    for js_file in sorted(FRONTEND_DIR.glob("*.js")):
        if js_file.name.endswith(".test.js"):
            continue
        text = js_file.read_text(encoding="utf-8", errors="replace")
        for match in _CALL_RE.finditer(text):
            raw_path = match.group(2) or match.group(3) or ""
            if not raw_path.startswith("/api/"):
                continue
            method = match.group(1).upper()
            # `${...}` interpolations become a path segment wildcard placeholder
            stem = re.sub(r"\$\{[^}]*\}", "*", raw_path.split("?", 1)[0])
            segments = tuple(seg for seg in stem.split("/") if seg)
            calls.append((method, segments, js_file.name))
    return calls


def _matches(call_segments: list[str], route_segments: list[str]) -> bool:
    if len(call_segments) != len(route_segments):
        return False
    for call_segment, route_segment in zip(call_segments, route_segments):
        if route_segment.startswith("{"):
            continue
        if call_segment == "*":
            continue
        if call_segment != route_segment:
            return False
    return True


def test_frontend_literal_api_calls_are_all_registered() -> None:
    routes = _registered_routes()
    assert routes, "the web app exposes no routes at all; the probe itself is broken"

    unresolved = [
        (method, "/" + "/".join(segments), source)
        for method, segments, source in _frontend_calls()
        if not any(route_method == method and _matches(list(segments), list(route_segments)) for route_method, route_segments in routes)
    ]

    assert not unresolved, "frontend calls paths with no mounted route: " + "; ".join(
        f"{method} {path} ({source})" for method, path, source in unresolved
    )


def test_probe_sees_the_exec_approval_lane() -> None:
    """Guard the probe: the fixed paths must be found here, or the test proves nothing."""
    calls = {"/".join(segments) for _method, segments, _source in _frontend_calls()}
    assert any("resources/tools/exec-approvals" in path for path in calls)
    assert not any(path.startswith("api/admin/") for path in calls)
