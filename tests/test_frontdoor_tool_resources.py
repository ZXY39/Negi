"""Frontdoor-lane resource tools build and forward the session key."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(relative.replace('/', '_'), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Service:
    def __init__(self):
        self.calls = []

    async def startup(self):
        return None

    async def deliver_frontdoor_node_message(self, **payload):
        self.calls.append(payload)
        return {'ok': True, 'results': []}

    def list_frontdoor_node_messages(self, session_id):
        _ = session_id
        return []


@pytest.mark.asyncio
async def test_send_message_resource_builds_and_forwards_session():
    module = _load('tools/send_message/main/tool.py')
    service = _Service()
    runtime = SimpleNamespace(services=SimpleNamespace(main_task_service=service))
    tool = module.build(runtime)
    assert tool is not None
    raw = await tool.execute(
        to='node:c',
        content='hello',
        __g3ku_runtime={'session_key': 'web:test'},
    )
    payload = json.loads(raw)
    assert payload['ok'] is True
    assert service.calls[0]['session_id'] == 'web:test'
    assert service.calls[0]['to'] == 'node:c'


@pytest.mark.asyncio
async def test_history_resource_returns_empty_without_messages():
    module = _load('tools/view_message_history/main/tool.py')
    service = _Service()
    runtime = SimpleNamespace(services=SimpleNamespace(main_task_service=service))
    tool = module.build(runtime)
    assert tool is not None
    raw = await tool.execute(__g3ku_runtime={'session_key': 'web:test'})
    payload = json.loads(raw)
    assert payload['count'] == 0
