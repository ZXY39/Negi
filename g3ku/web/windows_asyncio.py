from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)
_DISCONNECT_WINERRORS = {10053, 10054}


def is_benign_windows_connection_reset(context: dict[str, Any] | None) -> bool:
    if os.name != 'nt':
        return False
    payload = context if isinstance(context, dict) else {}
    exc = payload.get('exception')
    if not isinstance(exc, ConnectionResetError):
        return False
    winerror = getattr(exc, 'winerror', None)
    errno = getattr(exc, 'errno', None)
    if winerror not in _DISCONNECT_WINERRORS and errno not in _DISCONNECT_WINERRORS:
        return False
    message = str(payload.get('message') or '').strip()
    handle_text = repr(payload.get('handle')) if payload.get('handle') is not None else ''
    transport_text = repr(payload.get('transport')) if payload.get('transport') is not None else ''
    combined = f'{message} {handle_text} {transport_text}'
    if '_call_connection_lost' not in combined:
        return False
    return '_ProactorBasePipeTransport' in combined or 'PipeTransport' in combined


def install_windows_connection_reset_filter(
    loop: asyncio.AbstractEventLoop | None = None,
) -> Callable[[], None]:
    target_loop = loop or asyncio.get_running_loop()
    previous = target_loop.get_exception_handler()

    def _handler(active_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if is_benign_windows_connection_reset(context):
            _LOGGER.debug('Suppressed benign Windows Proactor connection reset during websocket shutdown')
            return
        if previous is not None:
            previous(active_loop, context)
            return
        active_loop.default_exception_handler(context)

    target_loop.set_exception_handler(_handler)

    def _restore() -> None:
        if target_loop.get_exception_handler() is _handler:
            target_loop.set_exception_handler(previous)

    return _restore
