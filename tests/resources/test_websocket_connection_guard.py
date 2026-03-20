from __future__ import annotations

import os

import pytest

from g3ku.web.windows_asyncio import is_benign_windows_connection_reset
from main.api.websocket_utils import (
    WebSocketChannelClosed,
    is_disconnect_exception,
    websocket_receive_json,
    websocket_receive_text,
    websocket_send_json,
)


class _WindowsConnectionReset(ConnectionResetError):
    def __init__(self, winerror: int = 10054) -> None:
        super().__init__(winerror, 'peer reset')
        self.winerror = winerror


class _ResetOnSendSocket:
    async def send_json(self, payload):
        _ = payload
        raise _WindowsConnectionReset()


class _ResetOnReceiveJsonSocket:
    async def receive_json(self):
        raise _WindowsConnectionReset()


class _ResetOnReceiveTextSocket:
    async def receive_text(self):
        raise _WindowsConnectionReset()


class _ProactorHandle:
    def __repr__(self) -> str:
        return '<Handle _ProactorBasePipeTransport._call_connection_lost(None)>'


@pytest.mark.asyncio
async def test_websocket_send_json_converts_connection_reset_to_closed() -> None:
    with pytest.raises(WebSocketChannelClosed):
        await websocket_send_json(_ResetOnSendSocket(), {'ok': True})


@pytest.mark.asyncio
async def test_websocket_receive_json_converts_connection_reset_to_closed() -> None:
    with pytest.raises(WebSocketChannelClosed):
        await websocket_receive_json(_ResetOnReceiveJsonSocket())


@pytest.mark.asyncio
async def test_websocket_receive_text_converts_connection_reset_to_closed() -> None:
    with pytest.raises(WebSocketChannelClosed):
        await websocket_receive_text(_ResetOnReceiveTextSocket(), timeout=0.01)


def test_disconnect_exception_recognizes_runtime_disconnect_marker() -> None:
    error = RuntimeError('Unexpected ASGI message after websocket close message has been sent.')
    assert is_disconnect_exception(error) is True


def test_benign_windows_connection_reset_detection_is_narrow() -> None:
    context = {
        'message': 'Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)',
        'exception': _WindowsConnectionReset(),
        'handle': _ProactorHandle(),
    }
    expected = os.name == 'nt'
    assert is_benign_windows_connection_reset(context) is expected
    assert is_benign_windows_connection_reset({'message': 'other', 'exception': RuntimeError('boom')}) is False
