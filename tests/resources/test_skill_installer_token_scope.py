from __future__ import annotations

import importlib.util
import urllib.request
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    module_path = REPO_ROOT / "tools" / "skill-installer" / "main" / "tool.py"
    module_name = f"test_skill_installer_token_scope_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = _load_module()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/repo/tree/main/skills/demo", True),
        ("https://api.github.com/repos/owner/repo", True),
        ("https://codeload.github.com/owner/repo/zip/main", True),
        ("https://raw.githubusercontent.com/owner/repo/main/SKILL.md", True),
        ("https://objects.githubusercontent.com/github-production-release-asset-2e65be/12345", True),
        ("http://github.com/owner/repo", True),  # scheme irrelevant
        ("https://GITHUB.com/owner/repo", True),  # hostname case-insensitive
        ("https://evil.example.com/steal", False),
        ("https://github.com.attacker.example/steal", False),  # subdomain is not github.com
        ("https://github.com.evil.example/steal", False),
        ("not-a-url", False),
        ("", False),
    ],
)
def test_host_allows_token(url: str, expected: bool) -> None:
    assert module._host_allows_token(url) is expected


def _original_request(url: str, *, with_auth: bool = True) -> urllib.request.Request:
    headers = {
        "User-Agent": "g3ku-skill-installer/1.0",
        "Accept": "application/octet-stream, */*;q=0.1",
    }
    if with_auth:
        headers["Authorization"] = "Bearer sekret"
    return urllib.request.Request(url, headers=headers)


def test_redirect_to_foreign_host_strips_authorization() -> None:
    handler = module._TokenScopedRedirectHandler()
    original = _original_request("https://codeload.github.com/owner/repo/zip/main")
    rebuilt = handler.redirect_request(original, None, 302, "Found", {}, "https://evil.example.com/steal")
    assert rebuilt is not None
    assert rebuilt.full_url == "https://evil.example.com/steal"
    assert rebuilt.get_header("Authorization") is None
    # benign headers survive so the rebuilt request is still a valid HTTP request
    assert rebuilt.get_header("User-agent") == "g3ku-skill-installer/1.0"


def test_redirect_to_allowed_host_keeps_authorization() -> None:
    handler = module._TokenScopedRedirectHandler()
    original = _original_request("https://codeload.github.com/owner/repo/zip/main")
    rebuilt = handler.redirect_request(
        original, None, 302, "Found", {}, "https://codeload.github.com/owner/repo/zip/main/2"
    )
    assert rebuilt is not None
    assert rebuilt.get_header("Authorization") == "Bearer sekret"


class _FakeResponse:
    def __init__(self) -> None:
        self._sent = False

    def read(self, size: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return b"payload"

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    def __init__(self) -> None:
        self.captured: list[tuple[urllib.request.Request, int | None]] = []

    def open(self, request: urllib.request.Request, timeout: int | None = None) -> _FakeResponse:
        self.captured.append((request, timeout))
        return _FakeResponse()


@pytest.mark.parametrize(
    ("url", "expected_auth"),
    [
        ("https://codeload.github.com/owner/repo/zip/main", "Bearer sekret"),
        ("https://github.com/owner/repo/zip/main", "Bearer sekret"),
        ("https://evil.example.com/o/r.zip", None),
        ("https://github.com.attacker.example/o/r.zip", None),
    ],
)
def test_request_attaches_token_only_for_allowed_hosts(monkeypatch: pytest.MonkeyPatch, url: str, expected_auth: str | None) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "sekret")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    fake_opener = _FakeOpener()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: fake_opener)

    assert module._request(url, timeout=5) == b"payload"
    (captured, _timeout), = fake_opener.captured
    assert captured.get_header("Authorization") == expected_auth


def test_request_uses_gh_token_and_omits_auth_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "alt-sekret")
    fake_opener = _FakeOpener()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: fake_opener)

    module._request("https://codeload.github.com/o/r/zip/main", timeout=5)
    (captured, _timeout), = fake_opener.captured
    assert captured.get_header("Authorization") == "Bearer alt-sekret"

    monkeypatch.delenv("GH_TOKEN", raising=False)
    fake_opener.captured.clear()
    module._request("https://codeload.github.com/o/r/zip/main", timeout=5)
    (captured, _timeout), = fake_opener.captured
    assert captured.get_header("Authorization") is None