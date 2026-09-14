"""CEO 会话目录构建的事件循环卸载层。

`build_ceo_session_catalog` 需要遍历全部 CEO/渠道会话——含数十 MB 的转录
文件（冷缓存时逐行 JSON 解析）。它发生在 `/ws/ceo` 连接（每次重连两次）
与 `GET /api/ceo/sessions` 等路径上。若留在事件循环里同步执行，期间整个
web 服务停摆：任务大厅的 `/api/tasks`、`worker-status`、任务列表 WS 握手
全部挂起——即"切回任务大厅卡顿"的根因（浏览器标签页休眠唤醒后 CEO WS
重连，冷缓存目录构建独占事件循环数秒到数十秒）。

本模块提供：

- 专用单线程执行器 `run_off_event_loop`：目录构建/转录加载统一在此串行
  执行，事件循环只做 `await`，不再被同步 CPU/磁盘工作独占。
- 短 TTL 目录缓存 + 代际号：一次 WS 连接内的两次目录构建、以及紧随其后的
  发布/REST 调用直接命中缓存；写侧（新建/重命名/删除会话）经
  `store_ceo_catalog_cache` 刷新缓存并提升代际，使在途异步构建的过期结果
  不会被写回。

注意：`SessionManager` 无内置并发保护，目录构建又在工作线程读取会话对象，
因此 `SessionManager.get_or_create` 的加载路径加了锁，避免同一会话被两个
线程重复加载成两个分叉对象。
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from g3ku.runtime.web_ceo_sessions import build_ceo_session_catalog

_T = TypeVar('_T')

# 单线程：转录加载/目录构建彼此串行，等价于过去"都在事件循环上依次执行"
# 的语义，只是不再阻塞事件循环。
_CEO_SESSION_IO_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='ceo-session-io',
)

_CATALOG_CACHE_TTL_SECONDS = 3.0
_catalog_cache_lock = threading.Lock()
# active_session_id -> (monotonic_built_at, catalog)
_catalog_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# 代际号：写侧刷新缓存时递增，废弃所有在途异步构建的存储意图。
_catalog_generation = 0


async def run_off_event_loop(func: Callable[[], _T]) -> _T:
    """在 CEO 会话 IO 专用线程执行同步工作，事件循环仅等待结果。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_CEO_SESSION_IO_EXECUTOR, func)


def peek_ceo_catalog_cache(active_session_id: str) -> dict[str, Any] | None:
    """TTL 内命中返回目录，否则 None（不触发构建）。"""
    key = str(active_session_id or '').strip()
    with _catalog_cache_lock:
        entry = _catalog_cache.get(key)
        if entry is None:
            return None
        built_at, catalog = entry
        if time.monotonic() - built_at > _CATALOG_CACHE_TTL_SECONDS:
            return None
        return catalog


def store_ceo_catalog_cache(active_session_id: str, catalog: dict[str, Any]) -> None:
    """写侧同步构建完成后调用：写入缓存并提升代际。

    提升代际会使所有在途异步构建（构建开始于本次写操作之前）放弃写回，
    防止过期目录覆盖写侧刚产出的新目录。
    """
    global _catalog_generation
    key = str(active_session_id or '').strip()
    with _catalog_cache_lock:
        _catalog_generation += 1
        if isinstance(catalog, dict):
            _catalog_cache[key] = (time.monotonic(), catalog)


def invalidate_ceo_catalog_cache() -> None:
    global _catalog_generation
    with _catalog_cache_lock:
        _catalog_generation += 1
        _catalog_cache.clear()


async def build_ceo_session_catalog_async(
    session_manager: Any,
    *,
    active_session_id: str,
    is_running_resolver: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """事件循环友好版目录构建：TTL 命中直接返回，未命中在专用线程构建。"""
    cached = peek_ceo_catalog_cache(active_session_id)
    if cached is not None:
        return cached
    key = str(active_session_id or '').strip()
    with _catalog_cache_lock:
        generation = _catalog_generation
    catalog = await run_off_event_loop(
        lambda: build_ceo_session_catalog(
            session_manager,
            active_session_id=active_session_id,
            is_running_resolver=is_running_resolver,
        )
    )
    with _catalog_cache_lock:
        if generation == _catalog_generation:
            _catalog_cache[key] = (time.monotonic(), catalog)
    return catalog


def build_ceo_session_catalog_cached(
    session_manager: Any,
    *,
    active_session_id: str,
    is_running_resolver: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """同步上下文（发布路径）用：TTL 命中免构建，未命中就地构建并写缓存。"""
    cached = peek_ceo_catalog_cache(active_session_id)
    if cached is not None:
        return cached
    catalog = build_ceo_session_catalog(
        session_manager,
        active_session_id=active_session_id,
        is_running_resolver=is_running_resolver,
    )
    store_ceo_catalog_cache(active_session_id, catalog)
    return catalog
