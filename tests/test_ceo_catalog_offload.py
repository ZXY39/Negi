"""CEO 目录构建卸载层的回归测试。

覆盖返回任务大厅卡顿的根因修复：/ws/ceo 与 /api/ceo/sessions 的目录构建
离开事件循环（专用线程 + 短 TTL 缓存 + 代际失效），以及
SessionManager.get_or_create 在工作线程并发加载下的单实例保证。
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime import ceo_catalog_offload
from g3ku.runtime.ceo_catalog_offload import (
    build_ceo_session_catalog_async,
    build_ceo_session_catalog_cached,
    invalidate_ceo_catalog_cache,
    peek_ceo_catalog_cache,
    store_ceo_catalog_cache,
)
from g3ku.runtime.web_ceo_sessions import build_ceo_session_catalog
from g3ku.session.manager import SessionManager


@pytest.fixture(autouse=True)
def _clear_catalog_cache():
    invalidate_ceo_catalog_cache()
    yield
    invalidate_ceo_catalog_cache()


class _Session:
    def __init__(self, key: str, content: str) -> None:
        self.key = key
        self.messages = [{"role": "assistant", "content": content}]
        self.metadata = {}
        self.created_at = datetime(2026, 3, 21, 10, 0, 0)
        self.updated_at = datetime(2026, 3, 21, 10, 5, 0)


class _Store:
    def __init__(self) -> None:
        self._sessions = {"web:shared": _Session("web:shared", "local reply")}
        self.load_calls: list[str] = []

    def list_sessions(self):
        return [{"key": key} for key in self._sessions]

    def get_or_create(self, key: str):
        self.load_calls.append(key)
        return self._sessions[key]

    def save(self, _session) -> None:
        return None


def _patch_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "g3ku.runtime.web_ceo_sessions.load_config",
        lambda: SimpleNamespace(workspace_path=str(tmp_path)),
    )


@pytest.mark.asyncio
async def test_async_catalog_matches_sync_and_hits_cache(monkeypatch, tmp_path: Path) -> None:
    _patch_workspace(monkeypatch, tmp_path)
    store = _Store()

    sync_catalog = build_ceo_session_catalog(store, active_session_id="web:shared")
    async_catalog = await build_ceo_session_catalog_async(store, active_session_id="web:shared")

    assert [item.get("session_id") for item in async_catalog["items"]] == [
        item.get("session_id") for item in sync_catalog["items"]
    ]
    # 构建结果已进入 TTL 缓存：重复调用直接命中，不再触发目录构建。
    assert peek_ceo_catalog_cache("web:shared") is async_catalog
    before_calls = len(store.load_calls)
    again = await build_ceo_session_catalog_async(store, active_session_id="web:shared")
    assert again is async_catalog
    assert len(store.load_calls) == before_calls


def test_cached_sync_builder_uses_cache_and_populates_it(monkeypatch, tmp_path: Path) -> None:
    _patch_workspace(monkeypatch, tmp_path)
    store = _Store()

    first = build_ceo_session_catalog_cached(store, active_session_id="web:shared")
    calls_after_first = len(store.load_calls)
    second = build_ceo_session_catalog_cached(store, active_session_id="web:shared")
    assert second is first
    assert len(store.load_calls) == calls_after_first


@pytest.mark.asyncio
async def test_inflight_async_build_does_not_overwrite_newer_write(monkeypatch, tmp_path: Path) -> None:
    """在途异步构建开始后有写侧刷新缓存（代际提升）时，构建结果不得写回。"""
    _patch_workspace(monkeypatch, tmp_path)
    store = _Store()
    fresh_catalog = {"items": [{"session_id": "web:shared", "fresh": True}]}

    original_build = ceo_catalog_offload.build_ceo_session_catalog

    def _build_then_simulate_write(*args, **kwargs):
        # 模拟：构建进行到一半时，新建/重命名会话的写侧产出了新目录。
        store_ceo_catalog_cache("web:shared", fresh_catalog)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(ceo_catalog_offload, "build_ceo_session_catalog", _build_then_simulate_write)

    stale_result = await build_ceo_session_catalog_async(store, active_session_id="web:shared")
    # 调用方拿到的仍是本次构建结果（可用），但缓存必须是写侧的新目录。
    assert isinstance(stale_result, dict)
    assert peek_ceo_catalog_cache("web:shared") is fresh_catalog


def test_store_cache_invalidates_previous_entries() -> None:
    store_ceo_catalog_cache("web:x", {"marker": 1})
    assert peek_ceo_catalog_cache("web:x") == {"marker": 1}
    store_ceo_catalog_cache("web:x", {"marker": 2})
    assert peek_ceo_catalog_cache("web:x") == {"marker": 2}
    invalidate_ceo_catalog_cache()
    assert peek_ceo_catalog_cache("web:x") is None


def test_session_manager_get_or_create_loads_once_under_threads(tmp_path: Path) -> None:
    """get_or_create 并发调用只允许加载一次，且返回同一对象。

    目录构建在工作线程读取会话，事件循环线程也可能同时 get_or_create；
    双重加载会产生两个分叉对象，后写入者丢更新。
    """
    seed = SessionManager(tmp_path)
    session = seed.get_or_create("web:ceo-t1")
    session.messages.append({"role": "assistant", "content": "hello"})
    seed.save(session)

    manager = SessionManager(tmp_path)
    loads: list[str] = []
    original_load = manager._load

    def counting_load(key: str):
        loads.append(key)
        return original_load(key)

    manager._load = counting_load

    results: list = []
    results_lock = threading.Lock()

    def worker() -> None:
        item = manager.get_or_create("web:ceo-t1")
        with results_lock:
            results.append(item)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 8
    assert len(loads) == 1
    assert all(item is results[0] for item in results)
    assert [message.get("content") for message in results[0].messages] == ["hello"]
