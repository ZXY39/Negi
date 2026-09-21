"""/api/content/* must answer dead refs with 4xx, not 500.

The web log panel filled up with "接口返回 500：/api/content/read": after disk
governance prunes a task's temp outputs, the node detail panel keeps hydrating
output refs whose bytes are gone. ``ContentNavigationService._resolve`` signals
that with FileNotFoundError, the routes let it escape, and
``audit_error_capture_middleware`` records every escaped exception as a
``web_api_5xx`` event -- so an expected cache miss looked like a server fault
and got re-requested on every panel render.

Contract: FileNotFoundError -> 404 content_not_found,
          ValueError -> 400 content_ref_invalid.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main.api.rest as api_rest
from g3ku.content.navigation import ContentNavigationService

PRUNED_REF = 'path:temp/tasks/task_dead/verify6_out.txt'


class _RealContentService:
    """Exposes the same content methods MainRuntimeService does, backed by a
    real ContentNavigationService so the exception types under test are the
    ones production actually raises."""

    def __init__(self, content_store: ContentNavigationService) -> None:
        self.content_store = content_store

    async def startup(self) -> None:
        return None

    def read_content(self, *, ref=None, path=None, view='canonical'):
        return self.content_store.read(ref=ref, path=path, view=view)

    def describe_content(self, *, ref=None, path=None, view='canonical'):
        return self.content_store.describe(ref=ref, path=path, view=view)

    def search_content(self, *, query, ref=None, path=None, view='canonical', limit=10, before=2, after=2):
        return self.content_store.search(
            query=query, ref=ref, path=path, view=view, limit=limit, before=before, after=after
        )

    def open_content(self, *, ref=None, path=None, view='canonical', start_line=None, end_line=None, around_line=None, window=None):
        return self.content_store.open(
            ref=ref,
            path=path,
            view=view,
            start_line=start_line,
            end_line=end_line,
            around_line=around_line,
            window=window,
        )


class _Harness(NamedTuple):
    client: TestClient
    workspace: Path


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    (tmp_path / 'temp' / 'tasks' / 'task_dead').mkdir(parents=True)
    service = _RealContentService(ContentNavigationService(workspace=tmp_path))
    monkeypatch.setattr(api_rest, '_service', lambda: service)
    app = FastAPI()
    app.include_router(api_rest.router, prefix='/api')
    return _Harness(TestClient(app, raise_server_exceptions=False), tmp_path)


def test_read_maps_pruned_temp_output_to_404(harness: _Harness) -> None:
    response = harness.client.get('/api/content/read', params={'ref': PRUNED_REF})
    assert response.status_code == 404
    detail = response.json()['detail']
    assert detail['code'] == 'content_not_found'
    assert 'verify6_out.txt' in detail['message']


def test_read_maps_pruned_artifact_record_to_404(harness: _Harness) -> None:
    response = harness.client.get('/api/content/read', params={'ref': 'artifact:835aefebc88d'})
    assert response.status_code == 404
    assert response.json()['detail']['code'] == 'content_not_found'


def test_read_maps_non_file_target_to_400(harness: _Harness) -> None:
    response = harness.client.get('/api/content/read', params={'path': 'temp/tasks/task_dead'})
    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'content_ref_invalid'


def test_read_still_returns_live_output(harness: _Harness) -> None:
    live = harness.workspace / 'temp' / 'tasks' / 'task_dead' / 'still_here.txt'
    live.write_text('live body', encoding='utf-8')
    response = harness.client.get('/api/content/read', params={'ref': 'path:temp/tasks/task_dead/still_here.txt'})
    assert response.status_code == 200
    assert response.json()['content'] == 'live body'


@pytest.mark.parametrize(
    ('route', 'params'),
    [
        ('/api/content/describe', {'ref': PRUNED_REF}),
        ('/api/content/open', {'ref': PRUNED_REF}),
        ('/api/content/search', {'ref': PRUNED_REF, 'query': 'needle'}),
    ],
)
def test_sibling_content_routes_map_dead_ref_to_404(harness: _Harness, route: str, params: dict) -> None:
    response = harness.client.get(route, params=params)
    assert response.status_code == 404
    assert response.json()['detail']['code'] == 'content_not_found'
