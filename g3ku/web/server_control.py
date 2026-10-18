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


def can_request_server_shutdown() -> bool:
    """退出这条道现在走不走得通——供调用方在**动手之前**问一次。

    `--reload` 车道由 uvicorn 的重载监督器自己起服务进程，本进程里没有可写的
    server 实例；先关 runtime 再发现退不掉，就会留下"端口还占着、runtime 已经没了"
    的半死进程。所以这条判断必须发生在暂停工作与关停之前。
    """
    with _SERVER_LOCK:
        return _SERVER_INSTANCE is not None


__all__ = ["can_request_server_shutdown", "request_server_shutdown", "set_server_instance"]
