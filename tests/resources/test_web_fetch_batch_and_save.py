"""web_fetch 批量抓取与结果落盘的合同测试。

覆盖点：
- 一次调用可传多条 URL，结果按提交顺序逐条回报。
- 每条成功结果的正文整份落到调用方 temp 根（或 save_dir 指定的锚点目录），文件名由 URL 派生，
  重抓覆盖同一份；内联 `text` 只是 `max_chars` 额度内的节选。
- 单条失败（SSRF/参数）只打在那一行，其余行照常交付；整批有失败时顶层 ok=false + partial。
- `url` 与 `urls` 合并去重；超过每次调用上限整单拒掉。
- 统一 timeout 约束整次调用墙钟：预算耗尽后还没开跑的 URL 报预算到期，不会一路追下去。
- 缓存命中不再走网络，但仍然落本次调用自己的文件（缓存里的路径属于别的会话）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

import tools.web_fetch.main.tool as tool_module
from tools.web_fetch.main.tool import WebFetchTool

_ARTICLE = "<html><body><main><p>" + ("body text. " * 400) + "</p></main></body></html>"
_SHORT = "<html><body><main><p>short page body</p></main></body></html>"
_PAGES = {
    "https://one.example.com/article": _ARTICLE,
    "https://two.example.org/": _SHORT,
    "https://three.example.net/docs/guide": _SHORT,
}


class _FakeResponse:
    def __init__(self, url: str, body: str) -> None:
        self.url = url
        self.status_code = 200
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.content = body.encode("utf-8")


class _Network:
    """假网络：记录调用次数，可加延迟模拟慢站。"""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.calls: list[str] = []
        self.delay = delay

    async def get(self, url: str, timeout: Any = None) -> _FakeResponse:
        self.calls.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        return _FakeResponse(url, _PAGES[url])

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self

        class _ClientFactory:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> Any:
                return client

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            async def get(self, url: str, timeout: Any = None) -> _FakeResponse:
                return await client.get(url, timeout=timeout)

        monkeypatch.setattr(tool_module.httpx, "AsyncClient", _ClientFactory)
        # SSRF 判定要真实 DNS，本文件按用例决定放行还是拦下。
        monkeypatch.setattr(tool_module, "_assert_url_is_safe", lambda url: None)


@pytest.fixture()
def temp_root(tmp_path: Path) -> Path:
    root = tmp_path / "temp" / "tasks" / "task-abc"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _runtime(temp_root: Path) -> dict[str, str]:
    return {"task_temp_dir": str(temp_root), "temp_dir": str(temp_root)}


def _runtime_kwargs(tool: WebFetchTool, runtime: dict[str, str]) -> dict[str, Any]:
    """取注入用的真实形参名：`__g3ku_runtime` 在工具类体内会被改名。"""
    for name in inspect.signature(tool.__call__).parameters:
        if name == "__g3ku_runtime" or name.endswith("__g3ku_runtime"):
            return {name: runtime}
    raise AssertionError("__g3ku_runtime parameter is missing from the tool signature")


async def test_batch_fetch_keeps_order_and_lands_one_file_per_url(tmp_path, temp_root, monkeypatch):
    network = _Network()
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)

    result = await tool(
        urls=["https://two.example.org/", "https://one.example.com/article"],
        include_text=False,
        **_runtime_kwargs(tool, _runtime(temp_root)),
    )

    assert [item["url"] for item in result["items"]] == [
        "https://two.example.org/",
        "https://one.example.com/article",
    ]
    assert result["ok"] is True and result["total"] == 2 and result["succeeded"] == 2
    assert result["saved_files"] == 2
    assert result["save_dir"] == str(temp_root / "web_fetch")
    for item in result["items"]:
        saved = Path(str(item["saved_path"]))
        assert saved.is_file()
        assert saved.read_text(encoding="utf-8").strip()
    assert network.calls == ["https://two.example.org/", "https://one.example.com/article"]


async def test_saved_file_holds_more_than_the_inline_excerpt(tmp_path, temp_root, monkeypatch):
    network = _Network()
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)

    result = await tool(
        url="https://one.example.com/article",
        max_chars=500,
        **_runtime_kwargs(tool, _runtime(temp_root)),
    )
    item = result["items"][0]

    assert len(item["text"]) <= 600
    assert item["text_chars_total"] > 2000
    saved = Path(str(item["saved_path"]))
    assert len(saved.read_text(encoding="utf-8")) == item["text_chars_total"]
    assert item["saved_truncated"] is False


async def test_include_text_false_keeps_bodies_off_the_context(tmp_path, temp_root, monkeypatch):
    network = _Network()
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)

    result = await tool(
        urls=["https://one.example.com/article"],
        include_text=False,
        **_runtime_kwargs(tool, _runtime(temp_root)),
    )
    item = result["items"][0]

    assert item["text"] == ""
    assert Path(str(item["saved_path"])).is_file()
    # 整条结果要留在运行时的内联额度里，否则模型只拿到 artifact ref 而不是路径表。
    assert len(json.dumps(result, ensure_ascii=False)) < 6000


async def test_one_failing_url_only_costs_its_own_row(tmp_path, temp_root, monkeypatch):
    network = _Network()
    network.install(monkeypatch)

    def _ssrf_gate(url: str) -> None:
        if "two." in url:
            raise ValueError("target host is blocked by SSRF policy")

    monkeypatch.setattr(tool_module, "_assert_url_is_safe", _ssrf_gate)
    tool = WebFetchTool(workspace=tmp_path)

    result = await tool(
        urls=["https://one.example.com/article", "https://two.example.org/"],
        include_text=False,
        **_runtime_kwargs(tool, _runtime(temp_root)),
    )

    assert result["ok"] is False
    assert result["partial"] is True
    assert (result["succeeded"], result["failed"], result["saved_files"]) == (1, 1, 1)
    failed_item = result["items"][1]
    assert failed_item["ok"] is False
    assert "SSRF" in failed_item["error"]
    assert failed_item["saved_path"] is None


async def test_url_and_urls_are_merged_and_deduplicated(tmp_path, temp_root, monkeypatch):
    network = _Network()
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)

    result = await tool(
        url="https://two.example.org/",
        urls=["https://two.example.org/", "https://three.example.net/docs/guide"],
        include_text=False,
        **_runtime_kwargs(tool, _runtime(temp_root)),
    )

    assert result["total"] == 2
    assert network.calls == ["https://two.example.org/", "https://three.example.net/docs/guide"]


async def test_batch_over_the_per_call_limit_is_rejected_with_the_number(tmp_path, temp_root, monkeypatch):
    network = _Network()
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)
    too_many = [f"https://one.example.com/article?i={index}" for index in range(tool_module._MAX_URLS_PER_CALL + 1)]

    with pytest.raises(ValueError) as exc:
        await tool(
            urls=too_many,
            include_text=False,
            **_runtime_kwargs(tool, _runtime(temp_root)),
        )

    assert str(tool_module._MAX_URLS_PER_CALL) in str(exc.value)
    assert network.calls == []


async def test_missing_urls_are_rejected(tmp_path, temp_root, monkeypatch):
    network = _Network()
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)

    with pytest.raises(ValueError):
        await tool(urls=[], include_text=False, **_runtime_kwargs(tool, _runtime(temp_root)))
    with pytest.raises(ValueError):
        await tool(max_chars=600, include_text=False, **_runtime_kwargs(tool, _runtime(temp_root)))


async def test_saved_path_is_derived_from_the_url_and_stable_across_calls(tmp_path, temp_root, monkeypatch):
    network = _Network()
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)

    first = await tool(
        url="https://one.example.com/article",
        include_text=False,
        **_runtime_kwargs(tool, _runtime(temp_root)),
    )
    second = await tool(
        url="https://one.example.com/article",
        include_text=False,
        save_dir="{temp}/pages",
        **_runtime_kwargs(tool, _runtime(temp_root)),
    )

    first_path = Path(str(first["items"][0]["saved_path"]))
    second_path = Path(str(second["items"][0]["saved_path"]))
    assert first_path.name.startswith("one.example.com-article-")
    assert first_path.name.endswith(".txt")
    # 默认落 temp 根的 web_fetch 子目录；save_dir 换目录时同一 URL 仍同名，覆盖行为可预期。
    assert first_path.parent == temp_root / "web_fetch"
    assert second_path.parent == temp_root / "pages"
    assert second_path.name == first_path.name


async def test_save_dir_bare_name_resolves_under_the_task_temp_root(tmp_path, temp_root, monkeypatch):
    network = _Network()
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)

    result = await tool(
        url="https://two.example.org/",
        include_text=False,
        save_dir="scrape",
        **_runtime_kwargs(tool, _runtime(temp_root)),
    )

    assert Path(str(result["items"][0]["saved_path"])).parent == temp_root / "scrape"


async def test_save_dir_falls_back_to_workspace_temp_without_runtime(tmp_path, monkeypatch):
    network = _Network()
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)

    result = await tool(url="https://two.example.org/", include_text=False)

    assert result["save_dir"] == str(tmp_path / "temp" / "web_fetch")
    assert Path(str(result["items"][0]["saved_path"])).is_file()


async def test_unified_timeout_bounds_the_whole_call(tmp_path, temp_root, monkeypatch):
    urls = [
        f"https://one.example.com/article?i={index}"
        for index in range(tool_module._CONCURRENT_FETCHES + 1)
    ]
    _PAGES.update({url: _SHORT for url in urls})
    network = _Network(delay=0.2)
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)

    result = await tool(
        urls=urls,
        include_text=False,
        timeout_seconds=0.05,
        **_runtime_kwargs(tool, _runtime(temp_root)),
    )

    expired = [item for item in result["items"] if "budget expired" in str(item.get("error") or "")]
    assert expired, result["items"]
    assert result["failed"] == len(expired)


async def test_cache_hit_skips_the_network_and_still_lands_the_file(tmp_path, temp_root, monkeypatch):
    network = _Network()
    network.install(monkeypatch)
    tool = WebFetchTool(workspace=tmp_path)
    other_session = temp_root.parent / "task-other"
    runtime = _runtime(other_session)

    first = await tool(
        urls=["https://two.example.org/"],
        include_text=False,
        **_runtime_kwargs(tool, _runtime(temp_root)),
    )
    second = await tool(
        urls=["https://two.example.org/"],
        include_text=False,
        **_runtime_kwargs(tool, runtime),
    )

    assert first["items"][0]["cache"]["hit"] is False
    assert second["items"][0]["cache"]["hit"] is True
    assert network.calls == ["https://two.example.org/"]
    # 缓存里的正文可以复用，路径不行：另一个会话必须拿到自己 temp 根下的文件。
    second_saved = Path(str(second["items"][0]["saved_path"]))
    assert second_saved.is_file()
    assert second_saved.parent == other_session / "web_fetch"
