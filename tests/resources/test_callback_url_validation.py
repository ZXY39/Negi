from __future__ import annotations

import asyncio

import pytest

from main.service.runtime_service import CallbackUrlNotAllowedError, MainRuntimeService
from main.service.task_terminal_callback import is_allowed_callback_url


@pytest.mark.parametrize(
    ('url', 'expected_allowed'),
    [
        # https: any host allowed
        ('https://example.com/api/internal/task-terminal', True),
        ('https://evil.example.com/anything', True),
        ('https://127.0.0.1:8443/api', True),
        # http: loopback hosts allowed
        ('http://localhost:18790/api/internal/task-terminal', True),
        ('http://127.0.0.1:18790/api/internal/task-terminal', True),
        ('http://[::1]:18790/api/internal/task-terminal', True),
        ('http://web:18790/api/internal/task-terminal', True),
        ('http://WEB:18790/api', True),
        # http: non-loopback hosts rejected
        ('http://evil.com:18790/api/internal/task-terminal', False),
        ('http://10.0.0.5:18790/api', False),
        ('http://corp.local:18790/api', False),
        # malformed / missing host
        ('http:///api', False),
        ('http://[bad/url', False),
        ('', False),
        ('not-a-url', False),
        # non-http(s) schemes rejected
        ('https://allowed.example.com/some/path#frag', True),
        ('ftp://example.com/file', False),
        ('file:///etc/passwd', False),
        ('javascript:alert(1)', False),
    ],
)
def test_is_allowed_callback_url_table(url: str, expected_allowed: bool) -> None:
    allowed, reason = is_allowed_callback_url(url)
    assert isinstance(reason, str) and reason
    assert allowed is expected_allowed


def test_is_allowed_callback_url_http_hosts_env_appends(monkeypatch) -> None:
    monkeypatch.setenv('G3KU_CALLBACK_HTTP_ALLOWED_HOSTS', 'corp.local, worker-node, [::1]')
    assert is_allowed_callback_url('http://corp.local:18790/api')[0] is True
    assert is_allowed_callback_url('http://worker-node/api')[0] is True
    # env matching is case-insensitive
    assert is_allowed_callback_url('http://CORP.LOCAL/api')[0] is True
    # substring hosts are not allowed, only exact host matches
    assert is_allowed_callback_url('http://corp.evil.com/api')[0] is False
    assert is_allowed_callback_url('http://bot-worker-node/api')[0] is False
    # default loopback hosts remain allowed alongside env extras
    assert is_allowed_callback_url('http://localhost/api')[0] is True
    assert is_allowed_callback_url('http://[::1]/api')[0] is True


def test_is_allowed_callback_url_http_hosts_env_empty(monkeypatch) -> None:
    monkeypatch.setenv('G3KU_CALLBACK_HTTP_ALLOWED_HOSTS', '  , ')
    assert is_allowed_callback_url('http://localhost/api')[0] is True
    assert is_allowed_callback_url('http://evil.com/api')[0] is False


class _FakeCallbackClient:
    def __init__(self) -> None:
        self.post_calls: list[tuple[tuple, dict]] = []

    async def post(self, *args, **kwargs) -> None:
        self.post_calls.append((args, kwargs))
        return None


def _bare_runtime_service() -> MainRuntimeService:
    service = object.__new__(MainRuntimeService)
    service._callback_client = _FakeCallbackClient()
    return service


def test_invalid_callback_url_is_not_posted() -> None:
    service = _bare_runtime_service()
    url = 'http://evil.example.com:18790/api/internal/task-terminal'
    with pytest.raises(CallbackUrlNotAllowedError):
        asyncio.run(
            service._post_internal_callback(
                url,
                payload={'ok': True},
                headers={'x-g3ku-internal-token': 'secret'},
                timeout=2.0,
            )
        )
    assert service._callback_client.post_calls == []


def test_allowed_callback_url_is_posted_unchanged() -> None:
    service = _bare_runtime_service()
    url = 'http://localhost:18790/api/internal/task-terminal'
    asyncio.run(
        service._post_internal_callback(
            url,
            payload={'ok': True},
            headers={'x-g3ku-internal-token': 'secret'},
            timeout=2.0,
        )
    )
    assert len(service._callback_client.post_calls) == 1
    sent_url, sent_kwargs = service._callback_client.post_calls[0][0][0], service._callback_client.post_calls[0][1]
    assert sent_url == url
    assert sent_kwargs.get('json') == {'ok': True}
    assert sent_kwargs.get('headers') == {'x-g3ku-internal-token': 'secret'}
    assert sent_kwargs.get('timeout') == 2.0