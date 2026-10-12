"""Web 慢请求留痕：阈值门、/api 门、中间件真实走一遍只透传响应。"""

from types import SimpleNamespace

import pytest
from loguru import logger

from g3ku.web import main as web_main


@pytest.fixture()
def captured():
    lines: list[str] = []
    sink_id = logger.add(lambda message: lines.append(message.record["message"]), format="{message}", level="INFO")
    yield lines
    logger.remove(sink_id)


def test_slow_api_request_logs_one_line(captured) -> None:
    assert web_main._log_slow_web_request("get", "/api/tasks", 200, 1000.0) is True
    assert captured == ["slow web request: GET /api/tasks -> 200 in 1000 ms"]


def test_fast_request_and_non_api_paths_stay_silent(captured) -> None:
    assert web_main._log_slow_web_request("GET", "/api/tasks", 200, 999.9) is False
    assert web_main._log_slow_web_request("GET", "/index.html", 200, 5000.0) is False
    assert web_main._log_slow_web_request("GET", "/api", 500, 5000.0) is True
    assert web_main._log_slow_web_request("GET", "/api/ws/tasks", 200, 5000.0) is True
    assert [line for line in captured if "index.html" in line] == []
    assert len(captured) == 2


def test_threshold_is_exposed_as_module_constant() -> None:
    assert float(web_main._SLOW_REQUEST_LOG_MS) == 1000.0


@pytest.mark.asyncio()
async def test_middleware_passes_response_through_and_measures_it(captured, monkeypatch) -> None:
    monkeypatch.setattr(web_main, "_SLOW_REQUEST_LOG_MS", 0.0)
    sentinel = SimpleNamespace(status_code=200)

    async def call_next(_request):
        return sentinel

    request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/api/tasks/task:abc"))
    response = await web_main.slow_request_log_middleware(request, call_next)

    assert response is sentinel
    assert len(captured) == 1
    assert captured[0].startswith("slow web request: GET /api/tasks/task:abc -> 200 in ")


def test_slow_layer_is_outermost_of_the_three() -> None:
    # `@app.middleware` 把新项插进 user_middleware 头部，Starlette 构建时从后往前套，
    # 所以这个列表里**靠前的在外层**。计时层必须在最外层，否则 423 闸门与审计层的
    # 花销量不到，报出来的数比浏览器实际等的小。
    gates = {"slow_request_log_middleware", "bootstrap_lock_middleware", "audit_error_capture_middleware"}
    names = [getattr(entry.kwargs.get("dispatch"), "__name__", "") for entry in web_main.app.user_middleware]
    outermost = next(name for name in names if name in gates)
    assert outermost == "slow_request_log_middleware"
    assert gates.issubset(set(names))
