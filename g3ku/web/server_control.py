from __future__ import annotations

import threading

import uvicorn


_SERVER_LOCK = threading.RLock()
_SERVER_INSTANCE: uvicorn.Server | None = None


def set_server_instance(server: uvicorn.Server | None) -> None:
    global _SERVER_INSTANCE
    with _SERVER_LOCK:
        _SERVER_INSTANCE = server


def request_server_shutdown() -> bool:
    with _SERVER_LOCK:
        if _SERVER_INSTANCE is None:
            return False
        _SERVER_INSTANCE.should_exit = True
        return True


__all__ = ["request_server_shutdown", "set_server_instance"]
