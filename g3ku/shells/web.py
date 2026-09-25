"""Web shell runtime bootstrap for the converged runtime architecture."""

from __future__ import annotations

import os
import asyncio
import subprocess
import sys
import time
from typing import Any, Optional
from urllib.parse import urlparse

from loguru import logger

from g3ku.agent.loop import AgentLoop
from g3ku.bus.events import OutboundMessage
from g3ku.bus.queue import MessageBus
from g3ku.runtime.external_events import get_session_event_hub
from g3ku.runtime.external_outbox import (
    compact_outbox,
    expire_stale_pending,
    load_pending_outbound,
    record_age_seconds,
    record_outbound_message,
)
from g3ku.runtime.external_sessions import EXTERNAL_OUTBOUND_CHANNEL, get_external_session_registry
from g3ku.runtime.session_keys import (
    EXTERNAL_SESSION_KEY_PREFIX,
    sanitize_channel_outbound_text,
)
from g3ku.config.loader import get_data_dir
from g3ku.config.live_runtime import get_runtime_config
from g3ku.cron.runtime_dispatch import dispatch_cron_job
from g3ku.cron.service import CronService
from g3ku.heartbeat.bootstrap import build_web_session_heartbeat, start_web_session_heartbeat
from g3ku.runtime.bootstrap_factory import make_agent_loop as _make_agent_loop
from g3ku.runtime.bootstrap_factory import make_provider as _make_provider
from g3ku.runtime import SessionRuntimeBridge
from g3ku.runtime import SessionRuntimeManager
from g3ku.runtime.config_refresh import refresh_loop_runtime_config
from g3ku.security import get_bootstrap_security_service
from g3ku.web.launcher import run_web_server_entrypoint
from g3ku.web.worker_control import (
    auto_worker_enabled,
    ensure_managed_task_worker,
    keep_worker_enabled,
    managed_worker_pid,
    run_managed_task_worker_watchdog,
    shutdown_managed_task_worker,
)
from main.protocol import now_iso
from main.service.task_terminal_callback import TASK_TERMINAL_CALLBACK_URL_ENV

_global_agent: Optional[AgentLoop] = None
_global_bus: Optional[MessageBus] = None
_global_runtime_manager: Optional[SessionRuntimeManager] = None
_global_web_heartbeat = None
_global_outbound_drain_task: Optional[asyncio.Task] = None
_global_task_worker_watchdog_task: Optional[asyncio.Task] = None
_global_outbox_reconcile_task: Optional[asyncio.Task] = None
_global_qq_official_services: dict[str, Any] = {}
_global_runtime_services_lock: Optional[asyncio.Lock] = None
_global_qq_official_sync_lock: Optional[asyncio.Lock] = None

_NO_CEO_MODEL_CONFIGURED_MESSAGE = "No model configured for role 'ceo'."

# 服务端周期 outbox 对账：启动重放（_replay_pending_external_outbox）是一次性的，
# 覆盖不了「重启时账本为空、消息在重启后才产出」的窗口——2026-09-14 23:00 定时
# 日报正是这样滞留的：桥启动预热扑空 → 无 pump → 日报发布进 hub 时无订阅者，
# live 投递静默蒸发，而下一条恢复触发点（重启/入站）迟迟不来。对账循环把
# 「足够老且无订阅者」的 pending 记录重新注入总线，配合桥侧周期对账建 pump，
# 让滞留推送在分钟级自动补投，不再依赖重启。
OUTBOX_RECONCILE_INTERVAL_SECONDS = 60.0
# 重放年龄阈值：刚入账的记录留给 live 链路（订阅者可能正在建连），足够老才补投。
OUTBOX_REPUBLISH_MIN_AGE_SECONDS = 120.0
# 记录级重放退避：openai-compat 会话的 pending 记录永远没有 SSE 消费者，
# 无退避会每轮重注入一次直到 24h 过期；按记录指数退避（首值 60s、×2、封顶
# OUTBOX_REPUBLISH_MAX_BACKOFF_SECONDS）压制重复注入与日志噪声。
OUTBOX_REPUBLISH_INITIAL_BACKOFF_SECONDS = 60.0
OUTBOX_REPUBLISH_MAX_BACKOFF_SECONDS = 3600.0
# 稳态压实周期（~1h）：append-only 账本每条投递产生 msg+ack 两行，周期路径若
# 从不 compact 会无界增长，而它每分钟被对账读两次、每 30s 被桥轮询再读一次。
OUTBOX_COMPACT_EVERY_N_CYCLES = 60
# 每 N 个对账周期同步一次 qq-official 服务：sync_from_config 幂等，桥任务已死
# （崩溃置 error 后无自动重启路径）或配置签名变化时重建 → 崩溃桥 ~5 分钟自愈。
OUTBOX_BRIDGE_SYNC_EVERY_N_CYCLES = 5

# outbox_id -> (loop 时间戳 not_before, 当前退避秒数)；进程内状态，记录离开
# pending（ack/expire）即清理，重启清零（最多多注入一轮，符合 at-least-once）。
_outbox_republish_backoff: dict[str, tuple[float, float]] = {}


def is_no_ceo_model_configured_error(exc: BaseException | None) -> bool:
    return str(exc or '').strip() == _NO_CEO_MODEL_CONFIGURED_MESSAGE


def no_ceo_model_configured_payload() -> dict[str, str]:
    return {
        'code': 'no_model_configured',
        'message': '当前项目还没有配置可用模型。请先进入“模型配置”页面，新增并保存至少一个模型，并把它分配给主Agent（CEO）角色。',
    }


def _get_runtime_services_lock() -> asyncio.Lock:
    global _global_runtime_services_lock
    if _global_runtime_services_lock is None:
        _global_runtime_services_lock = asyncio.Lock()
    return _global_runtime_services_lock


def _get_qq_official_sync_lock() -> asyncio.Lock:
    global _global_qq_official_sync_lock
    if _global_qq_official_sync_lock is None:
        _global_qq_official_sync_lock = asyncio.Lock()
    return _global_qq_official_sync_lock


_PORT_OWNERSHIP_CACHE: dict[tuple[int, int], tuple[bool | None, float]] = {}
_PORT_OWNERSHIP_RETRY_AFTER_S = 10.0


def _listen_port_owners(port: int) -> set[int] | None:
    owners: set[int] = set()
    try:
        if os.name == 'nt':
            result = subprocess.run(
                ['netstat', '-ano', '-p', 'tcp'],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            if result.returncode != 0:
                return None
            needle = f':{int(port)}'
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                local_addr = parts[1]
                state = parts[3].upper()
                owning_pid = parts[4]
                if not local_addr.endswith(needle):
                    continue
                if state != 'LISTENING':
                    continue
                try:
                    owners.add(int(owning_pid))
                except Exception:
                    continue
            return owners

        commands = [
            ['ss', '-ltnp'],
            ['lsof', '-nP', f'-iTCP:{int(port)}', '-sTCP:LISTEN'],
        ]
        for command in commands:
            try:
                result = subprocess.run(command, capture_output=True, text=True, check=False)
            except FileNotFoundError:
                continue
            if result.returncode != 0:
                continue
            output = result.stdout
            if os.path.basename(command[0]) == 'ss':
                for line in output.splitlines():
                    if f':{int(port)}' not in line:
                        continue
                    for segment in line.split('pid=')[1:]:
                        pid_part = ''.join(ch for ch in segment if ch.isdigit())
                        if not pid_part:
                            continue
                        owners.add(int(pid_part))
                return owners
            for line in output.splitlines():
                if f':{int(port)}' not in line:
                    continue
                parts = line.split()
                for value in parts:
                    if value.isdigit():
                        owners.add(int(value))
                return owners
    except Exception:
        return None
    return None


def _process_owns_listen_port(port: int, *, pid: int | None = None) -> bool | None:
    # 端口归属探测要起 netstat/ss 子进程，在连接数多的机器上单次可达秒级。
    # 该检查只用于判定"本进程是否应启动 web cron"，而本进程持有的监听套接字
    # 在进程生命周期内不会易主，因此确认归属后永久缓存；未归属/探测失败则
    # 按短 TTL 重试。杜绝每个内部事件回调都在事件循环上同步起子进程。
    resolved_pid = int(pid or os.getpid())
    key = (int(port), resolved_pid)
    cached = _PORT_OWNERSHIP_CACHE.get(key)
    if cached is not None:
        owned, cached_at = cached
        if owned is True or (time.monotonic() - cached_at) < _PORT_OWNERSHIP_RETRY_AFTER_S:
            return owned
    owners = _listen_port_owners(port)
    owned = None if owners is None else (resolved_pid in owners)
    _PORT_OWNERSHIP_CACHE[key] = (owned, time.monotonic())
    return owned


def debug_trace_enabled() -> bool:
    raw = str(os.getenv('G3KU_DEBUG_TRACE', '')).strip().lower()
    return raw in {'1', 'true', 'yes', 'on', 'debug'}


def _resolve_web_runtime_port(agent: AgentLoop | None = None) -> int:
    callback_url = str(os.getenv(TASK_TERMINAL_CALLBACK_URL_ENV, "") or "").strip()
    if callback_url:
        try:
            parsed = urlparse(callback_url)
            if parsed.port:
                return int(parsed.port)
        except Exception:
            logger.debug("web cron port resolution skipped for callback url {}", callback_url)
    runtime_agent = agent or _global_agent
    config = getattr(runtime_agent, "app_config", None)
    return int(getattr(getattr(config, "web", None), "port", 18790) or 18790)


def _should_start_web_cron(agent: AgentLoop | None = None) -> bool:
    port = _resolve_web_runtime_port(agent)
    ownership = _process_owns_listen_port(port)
    if ownership is False:
        logger.debug(
            "Skipping web cron startup in pid={} because web port {} is owned by another process",
            os.getpid(),
            port,
        )
        return False
    return True


def _cron_runtime_ready(agent: AgentLoop | None = None) -> bool:
    runtime_agent = agent or _global_agent
    cron_service = getattr(runtime_agent, "cron_service", None) if runtime_agent is not None else None
    if cron_service is None:
        return True
    status = getattr(cron_service, "status", None)
    payload: dict[str, Any] = {}
    if callable(status):
        try:
            payload = status() or {}
        except Exception:
            payload = {}
    if bool(payload.get("enabled")):
        # cron 已由本进程启动并在运行：归属探测（子进程级开销）不再必要。
        # 先查状态再探测是本函数不被高频回调拖垮事件循环的关键顺序。
        return True
    if not _should_start_web_cron(runtime_agent):
        return True
    return False


def _build_web_cron_service(agent_holder: dict[str, AgentLoop]) -> CronService:
    async def _on_job(job) -> str | None:
        runtime_agent = agent_holder.get("agent")
        if runtime_agent is None:
            raise RuntimeError("web cron runtime is not initialized")
        runtime_bridge = SessionRuntimeBridge(get_runtime_manager(runtime_agent))
        task_registrar = getattr(runtime_agent, "_register_active_task", None)
        bus = _global_bus
        return await dispatch_cron_job(
            job,
            runtime_bridge=runtime_bridge,
            session_manager=getattr(runtime_agent, "sessions", None),
            register_task=task_registrar if callable(task_registrar) else None,
            publish_outbound=bus.publish_outbound if bus is not None else None,
        )

    return CronService(get_data_dir() / "cron" / "jobs.json", on_job=_on_job)


def get_agent() -> AgentLoop:
    global _global_agent, _global_bus, _global_runtime_manager, _global_web_heartbeat
    if not get_bootstrap_security_service().is_unlocked():
        raise RuntimeError('project is locked')
    if not _global_agent:
        config, revision, _changed = get_runtime_config(force=True)
        provider = _make_provider(config, scope='ceo')

        _global_bus = MessageBus()
        debug_mode = debug_trace_enabled()
        if debug_mode:
            logger.info('Web API debug trace enabled (G3KU_DEBUG_TRACE=1)')
        agent_holder: dict[str, AgentLoop] = {}
        cron_service = _build_web_cron_service(agent_holder)
        _global_agent = _make_agent_loop(
            config,
            _global_bus,
            provider,
            debug_mode=debug_mode,
            cron_service=cron_service,
        )
        agent_holder["agent"] = _global_agent
        _global_agent._runtime_model_revision = revision
        _global_agent._runtime_default_model_key = config.resolve_role_model_key('ceo')
        _global_runtime_manager = SessionRuntimeManager(_global_agent)
        _global_web_heartbeat = build_web_session_heartbeat(
            _global_agent,
            _global_runtime_manager,
            reply_notifier=_make_heartbeat_reply_notifier(),
        )
    elif _global_runtime_manager is None or _global_runtime_manager.loop is not _global_agent:
        _global_runtime_manager = SessionRuntimeManager(_global_agent)
        _global_web_heartbeat = build_web_session_heartbeat(
            _global_agent,
            _global_runtime_manager,
            reply_notifier=_make_heartbeat_reply_notifier(),
        )
    elif _global_web_heartbeat is None:
        _global_web_heartbeat = build_web_session_heartbeat(
            _global_agent,
            _global_runtime_manager,
            reply_notifier=_make_heartbeat_reply_notifier(),
        )
    return _global_agent


def peek_global_agent() -> Optional[AgentLoop]:
    """Return the live global agent WITHOUT constructing it.

    ``get_agent()`` lazily builds the agent (and its cron/heartbeat wiring) on
    first call; hooks that only want to observe an already-running web runtime
    — e.g. best-effort catalog fan-out, which must stay a no-op before the
    runtime is up and in unit tests without a runtime — use this instead.
    """
    return _global_agent


async def _cancel_background_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def refresh_web_agent_runtime(
    force: bool = False,
    reason: str = 'runtime',
    *,
    force_memory_sync: bool = False,
) -> bool:
    runtime_agent = get_agent()
    changed = refresh_loop_runtime_config(
        runtime_agent,
        force=force,
        reason=reason,
        force_memory_sync=force_memory_sync,
    )
    await _sync_qq_official_service()
    return changed


def qq_official_service_statuses() -> list[dict]:
    """每个已配置账号一行桥状态（admin REST 用）。"""
    rows: list[dict] = []
    for bridge_id, service in sorted(_global_qq_official_services.items()):
        status = service.status()
        rows.append({"bridge_id": bridge_id, "app_id": service.app_id, **status})
    return rows


async def _sync_qq_official_service() -> None:
    """Reconcile the per-account QQ official bridges with the current ``qqBot`` config.

    Lazy import: the adapter (and only the adapter) may pull in ``qq-botpy``,
    and only when a start actually happens inside ``sync_from_config``.

    整体持**一把**模块锁而不是每号一锁：diff 必须原子。sync_from_config 在
    ``_restart`` 的 ``await stop()`` 窗口内 ``_task is None``，并发调用（runtime
    refresh / 启动序列 / outbox 对账循环）会各自 create_task 一个桥，先建者成为无人
    持有的孤儿任务 → 双 botpy 连接、每条消息双份投递。锁序只有
    runtime-lock → sync-lock 单向嵌套，无死锁环。
    """
    global _global_qq_official_services
    from g3ku.config.loader import load_config
    from g3ku.qq_official.messages import bridge_id_for_app_id
    from g3ku.qq_official.service import QqOfficialService

    async with _get_qq_official_sync_lock():
        try:
            qq_bot = load_config().qq_bot
        except Exception:
            logger.exception("qq-official service sync skipped on config error")
            return
        global_enabled = bool(getattr(qq_bot, "enabled", False))
        desired: dict[str, tuple[str, Any]] = {}
        for app_id, account in dict(getattr(qq_bot, "accounts", None) or {}).items():
            app = str(app_id or "").strip()
            if not app or account is None:
                continue
            desired[bridge_id_for_app_id(app)] = (app, account)
        for bridge_id in list(_global_qq_official_services):
            if bridge_id in desired:
                continue
            removed = _global_qq_official_services.pop(bridge_id)
            try:
                await removed.stop()
            except Exception:
                logger.exception("qq-official bridge {} stop skipped", bridge_id)
        for bridge_id, (app_id, account) in desired.items():
            service = _global_qq_official_services.get(bridge_id)
            if service is None:
                service = QqOfficialService(app_id=app_id)
                _global_qq_official_services[bridge_id] = service
            try:
                await service.sync_from_config(account=account, global_enabled=global_enabled)
            except Exception:
                logger.exception("qq-official service sync skipped on error: {}", bridge_id)


def get_runtime_manager(agent: AgentLoop | None = None) -> SessionRuntimeManager:
    runtime_agent = agent or get_agent()
    global _global_runtime_manager
    if _global_runtime_manager is None or _global_runtime_manager.loop is not runtime_agent:
        _global_runtime_manager = SessionRuntimeManager(runtime_agent)
    return _global_runtime_manager


async def _notify_external_channel_reply(session_id: str, text: str) -> None:
    """Publish a heartbeat/cron/task-terminal reply for an external bridge
    session onto the outbound bus; the shared drain resolves the session via
    the external registry and routes it to the session event hub as
    ``outbound.created``."""
    bus = _global_bus
    if bus is None:
        return
    await bus.publish_outbound(
        OutboundMessage(
            channel=EXTERNAL_OUTBOUND_CHANNEL,
            chat_id=session_id,
            content=text,
            metadata={'source': 'heartbeat', 'session_key': session_id},
        )
    )


async def _notify_heartbeat_channel_reply(session_id: str, text: str) -> None:
    """Route a heartbeat/cron/task-terminal reply to its delivery channel.

    Only ``ext:`` sessions have a live delivery path now; legacy ``china:``
    sessions keep their transcripts but no longer receive proactive pushes
    (the China channel subsystem has been removed), so non-ext replies are
    skipped silently.
    """
    key = str(session_id or '').strip()
    payload = str(text or '').strip()
    if not payload:
        return
    if key.startswith(EXTERNAL_SESSION_KEY_PREFIX):
        await _notify_external_channel_reply(key, payload)
        return


def _make_heartbeat_reply_notifier():
    async def _notify(session_id: str, text: str) -> None:
        await _notify_heartbeat_channel_reply(session_id, text)

    return _notify


def _extract_outbound_media_attachments(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Lazy wrapper over ceo_media extraction (same circular-import constraint
    as ``g3ku.runtime.external_events``: ``g3ku.runtime.api`` imports this
    shell, so it must not be imported at module load here)."""
    from g3ku.runtime.api.ceo_media import extract_local_media_attachments

    cleaned, attachments = extract_local_media_attachments(text)
    return (cleaned if isinstance(cleaned, str) else text), list(attachments or [])


def _start_outbound_drain(bus: MessageBus) -> asyncio.Task:
    """Create the outbound drain task and return it.

    The drain is the only bridge between the in-process outbound bus and the
    external channel consumers (external bridge event hubs), so it must
    never die silently: a crashed drain strands every later message (e.g.
    cron reminders) in the queue forever with no log.

    Routing:
    - ``channel == "ext"``: resolve ``chat_id`` (a session key or a
      registered external_key) through the external session registry and
      publish ``outbound.created`` on that session's event hub. Unknown
      targets are dropped with a warning; internal-only (post-sanitize
      empty) text is acked silently, mirroring the transport contract.
    - Any other channel: the China channel subsystem has been removed, so
      non-ext outbound has no consumer. Surface it with a warning instead of
      dropping silently (the publisher almost certainly resolved the wrong
      channel, e.g. poisoned session meta).

    Failure handling:
    - Any exception is treated as a poison message: log and drop only that
      message, then keep draining.
    - ``CancelledError`` still terminates the task (shutdown/restart path).
    """

    async def _route_external_outbound(pending: OutboundMessage) -> None:
        sanitized = sanitize_channel_outbound_text(str(getattr(pending, "content", "") or ""))
        if not sanitized:
            return
        entry = get_external_session_registry().find_by_any_key(getattr(pending, "chat_id", None))
        if entry is None:
            logger.warning(
                "external outbound drain dropped message: unknown target chat_id={}",
                getattr(pending, "chat_id", "?"),
            )
            return
        metadata = getattr(pending, "metadata", None) or {}
        reply_to = str(getattr(pending, "reply_to", "") or "").strip()
        dedupe_key = str(metadata.get("dedupe_key") or "").strip()
        # 附件：重放消息自带（账本持久化过的）直接透传；首次出站从正文提取
        # （markdown 本地文件链接 → 结构化附件，正文留下文件名标签）。重放正文
        # 已是提取后文本，二次提取是幂等空操作。
        carried = metadata.get("attachments")
        if isinstance(carried, list):
            attachments = [item for item in carried if isinstance(item, dict)]
        else:
            sanitized, attachments = _extract_outbound_media_attachments(sanitized)
        # 持久 outbox 登记（先于 hub 发布）：桥 pump 断连或进程重启窗口里滞留的
        # 主动推送靠它在启动重放时找回。重放消息自带 outbox_id，直接复用不重复
        # 登记；登记失败（磁盘满）降级为仅内存投递，不阻断发布。
        outbox_id = str(metadata.get("outbox_id") or "").strip()
        if not outbox_id:
            outbox_id = record_outbound_message(
                session_key=entry.session_key,
                external_key=entry.external_key,
                text=sanitized,
                reply_to=reply_to,
                dedupe_key=dedupe_key,
                attachments=attachments or None,
            )
        payload: dict[str, Any] = {
            "text": sanitized,
            "external_key": entry.external_key,
            "session_key": entry.session_key,
        }
        if attachments:
            payload["attachments"] = attachments
        if reply_to:
            payload["reply_to"] = reply_to
        if dedupe_key:
            payload["dedupe_key"] = dedupe_key
        if outbox_id:
            payload["outbox_id"] = outbox_id
        hub = get_session_event_hub(entry.session_key)
        hub.publish("outbound.created", **payload)
        # 语义要精确：这一行只代表"事件已发布到该会话的内存 hub"，不代表已送达
        # 渠道——真正送达以桥侧 "qq-official delivered ..." 回执日志为准。
        # 无订阅者 = live 投递必然蒸发（2026-09-14 日报滞留事故的静默失败点），
        # 升级为 WARNING 便于 grep；补投靠下面的周期 outbox 对账兜底。
        if hub.subscriber_count() == 0:
            logger.warning(
                "external outbound published to hub with no live subscriber: "
                "session={} outbox_id={} (delivery deferred to outbox reconcile)",
                entry.session_key,
                outbox_id or "-",
            )
        else:
            logger.info(
                "external outbound published to hub: session={} outbox_id={}",
                entry.session_key,
                outbox_id or "-",
            )

    async def _drain_outbound() -> None:
        pending: OutboundMessage | None = None
        while True:
            try:
                if pending is None:
                    try:
                        pending = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                if pending.channel == EXTERNAL_OUTBOUND_CHANNEL:
                    await _route_external_outbound(pending)
                    pending = None
                    continue
                logger.warning(
                    "outbound drain skipped non-external message channel={} chat_id={}",
                    pending.channel,
                    pending.chat_id,
                )
                pending = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "outbound drain dropped message channel={} chat_id={}: {}",
                    getattr(pending, "channel", "?"),
                    getattr(pending, "chat_id", "?"),
                    exc,
                )
                pending = None

    return asyncio.create_task(_drain_outbound())


def _ensure_outbound_drain_running() -> None:
    """Start the shared outbound drain once the bus exists."""
    global _global_outbound_drain_task
    if _global_bus is None:
        return
    if _global_outbound_drain_task is None or _global_outbound_drain_task.done():
        _global_outbound_drain_task = _start_outbound_drain(_global_bus)


def _outbox_replay_message(record: dict[str, Any]) -> OutboundMessage:
    """Build the bus message that re-injects one ledger record with its
    original outbox_id (drain reuses the id, no duplicate registration).
    Persisted attachments ride along in metadata so the drain passes them
    through instead of re-extracting from the already-cleaned text."""
    metadata: dict[str, Any] = {
        "source": "outbox_replay",
        "outbox_id": str(record.get("id") or ""),
        "dedupe_key": str(record.get("dedupe_key") or ""),
    }
    attachments = record.get("attachments")
    if isinstance(attachments, list) and attachments:
        metadata["attachments"] = [item for item in attachments if isinstance(item, dict)]
    return OutboundMessage(
        channel=EXTERNAL_OUTBOUND_CHANNEL,
        chat_id=str(record.get("session_key") or ""),
        content=str(record.get("text") or ""),
        reply_to=str(record.get("reply_to") or "") or None,
        metadata=metadata,
    )


async def _replay_pending_external_outbox() -> None:
    """Replay the durable external outbox at startup.

    bus/hub 都是纯内存：桥 pump 断连或进程重启窗口里滞留的主动推送（心跳升级、
    cron 提醒、任务终态）随内存清空而蒸发。drain 在发布 hub 前已把每条消息登记
    进 ``.g3ku/external-outbox/``（见 ``g3ku/runtime/external_outbox.py``）；
    这里把时效窗口内未 ack 的条目带原 outbox_id 重新注入出站总线（drain 复用
    该 id，不会重复登记），过期条目标记 expired，然后压实账本。桥侧启动时按
    ``GET /outbox/pending`` 预热这些会话的 pump，消息经 SSE 重放完成投递后由
    桥 ack 销账（at-least-once：ack 丢失会在下次重启后重复投递一次）。

    启动重放是一次性的；重启后才产出的滞留推送由 ``_outbox_reconcile_loop``
    的周期对账兜底（见模块顶部常量注释）。
    """
    bus = _global_bus
    if bus is None:
        return
    try:
        expired = expire_stale_pending()
        pending = load_pending_outbound()
        for record in pending:
            await bus.publish_outbound(_outbox_replay_message(record))
        compact_outbox()
        if pending or expired:
            logger.warning(
                "external outbox replay: republished {} pending message(s), expired {}",
                len(pending),
                expired,
            )
    except Exception:
        logger.exception("external outbox replay skipped on error")


async def _reconcile_external_outbox_once() -> tuple[int, int]:
    """One periodic reconcile pass; returns ``(republished, expired)``.

    与启动重放的差别：只重放「年龄 > OUTBOX_REPUBLISH_MIN_AGE_SECONDS 且对应
    会话 hub 当前无订阅者」的记录——有订阅者说明 pump/等待方在线，ring buffer
    的 Last-Event-ID 重放已兜底，再注入只会制造重复副本；按记录指数退避压制
    永久无消费者会话（openai-compat 走同一账本但从不开 SSE）的重复注入。
    过期清理每轮都跑（不再依赖重启）。压实由调用方按周期决定。
    """
    bus = _global_bus
    if bus is None:
        return (0, 0)
    loop = asyncio.get_running_loop()
    now = loop.time()
    expired = expire_stale_pending()
    pending = load_pending_outbound()
    live_ids = {str(record.get("id") or "") for record in pending}
    for stale_id in [key for key in _outbox_republish_backoff if key not in live_ids]:
        _outbox_republish_backoff.pop(stale_id, None)
    republished = 0
    for record in pending:
        outbox_id = str(record.get("id") or "")
        age = record_age_seconds(record)
        if not outbox_id or age is None or age < OUTBOX_REPUBLISH_MIN_AGE_SECONDS:
            continue
        not_before, backoff = _outbox_republish_backoff.get(outbox_id, (0.0, 0.0))
        if now < not_before:
            continue
        session_key = str(record.get("session_key") or "")
        if not session_key or get_session_event_hub(session_key).subscriber_count() > 0:
            continue
        await bus.publish_outbound(_outbox_replay_message(record))
        next_backoff = (
            OUTBOX_REPUBLISH_INITIAL_BACKOFF_SECONDS
            if backoff <= 0
            else min(backoff * 2.0, OUTBOX_REPUBLISH_MAX_BACKOFF_SECONDS)
        )
        _outbox_republish_backoff[outbox_id] = (now + next_backoff, next_backoff)
        republished += 1
    return (republished, expired)


async def _outbox_reconcile_loop() -> None:
    """Periodic outbox reconcile: the recovery lane that does not depend on
    restarts. Sleep-first（启动路径已做过全量重放），逐轮异常守护对齐
    ``_drain_outbound``：单轮失败绝不杀循环。"""
    cycles = 0
    while True:
        try:
            await asyncio.sleep(OUTBOX_RECONCILE_INTERVAL_SECONDS)
            cycles += 1
            # drain 若已死，重放进总线无人路由：每轮幂等复活（done 检查早退）。
            _ensure_outbound_drain_running()
            republished, expired = await _reconcile_external_outbox_once()
            if republished or expired:
                logger.warning(
                    "external outbox reconcile: republished {} pending message(s), expired {}",
                    republished,
                    expired,
                )
                compact_outbox()  # 活动轮立即压实，收敛 tombstone 与重放副本
            elif cycles % OUTBOX_COMPACT_EVERY_N_CYCLES == 0:
                compact_outbox()  # 稳态每小时压实，防 append-only 账本无界增长
            if cycles % OUTBOX_BRIDGE_SYNC_EVERY_N_CYCLES == 0:
                await _sync_qq_official_service()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("external outbox reconcile pass skipped on error")


def _ensure_outbox_reconcile_running() -> None:
    """Start the periodic outbox reconcile loop once the bus exists."""
    global _global_outbox_reconcile_task
    if _global_bus is None:
        return
    if _global_outbox_reconcile_task is None or _global_outbox_reconcile_task.done():
        _global_outbox_reconcile_task = asyncio.create_task(
            _outbox_reconcile_loop(), name="external-outbox-reconcile"
        )


def _ensure_task_worker_watchdog_running(service: Any = None) -> None:
    """Start the managed task worker watchdog once (idempotent).

    The watchdog restarts the managed worker when it dies, so a transient
    startup failure (e.g. a not-yet-expired lease) no longer leaves the task
    hall permanently "stale".
    """
    global _global_task_worker_watchdog_task
    if not auto_worker_enabled():
        return
    if _global_task_worker_watchdog_task is None or _global_task_worker_watchdog_task.done():
        _global_task_worker_watchdog_task = asyncio.create_task(
            run_managed_task_worker_watchdog(service),
            name="task-worker-watchdog",
        )


_SHUTDOWN_PAUSE_DRAIN_TIMEOUT_S = 10.0
_SHUTDOWN_PAUSE_DRAIN_POLL_S = 0.1


async def wait_shutdown_pause_commands_drained(
    service: Any,
    *,
    task_ids: set[str],
    timeout_s: float = _SHUTDOWN_PAUSE_DRAIN_TIMEOUT_S,
) -> bool:
    """Wait until the worker has really applied the shutdown pause commands.

    Durable pause flags are written by the web process synchronously, but the
    worker's actor keeps running until it processes the `pause_task` command
    (which awaits the actor unwinding). Poll the shared command table so
    shutdown does not declare victory — or kill the worker — while a model
    call or long tool is still executing in the background.
    """
    normalized_ids = {str(item or '').strip() for item in list(task_ids or []) if str(item or '').strip()}
    store = getattr(service, 'store', None)
    list_unfinished = getattr(store, 'list_unfinished_task_commands', None)
    if not normalized_ids or not callable(list_unfinished):
        return True
    worker_state = getattr(service, 'worker_state', None)
    deadline = asyncio.get_running_loop().time() + max(0.0, float(timeout_s or 0.0))
    while asyncio.get_running_loop().time() < deadline:
        try:
            unfinished = [
                item
                for item in list_unfinished()
                if str(item.get('command_type') or '').strip() == 'pause_task'
                and str(item.get('task_id') or '').strip() in normalized_ids
            ]
        except Exception:
            return True
        if not unfinished:
            return True
        if callable(worker_state):
            try:
                state = str(worker_state() or '').strip().lower()
            except Exception:
                state = ''
            if state in {'stopped', 'offline', 'dead'}:
                # No live worker will drain these commands; the durable flags
                # plus the shutdown ledger already cover the restart.
                return True
            # 'online' / 'starting' / 'stale' keep waiting: a stale row may
            # still belong to a live but busy worker.
        await asyncio.sleep(_SHUTDOWN_PAUSE_DRAIN_POLL_S)
    logger.warning(
        "shutdown pause drain timed out after {:.1f}s; {} pause commander(s) still unfinished",
        float(timeout_s or 0.0),
        len(normalized_ids),
    )
    return False


async def pause_running_work_for_shutdown(
    agent: AgentLoop | None = None,
    runtime_manager: SessionRuntimeManager | None = None,
) -> dict[str, int]:
    """Pause every running session and in-progress task before a process exit.

    Manual pause archives each in-flight turn durably; task pause takes effect
    at the next safe boundary and is persisted immediately. Both kinds are
    recorded in the shutdown-pause ledger so the next startup can resume them
    silently instead of treating them as abnormal stops. User/agent-initiated
    pauses are deliberately not recorded and therefore never auto-resumed.
    """
    runtime_agent = agent if agent is not None else _global_agent
    current_manager = runtime_manager if runtime_manager is not None else _global_runtime_manager
    service = getattr(runtime_agent, "main_task_service", None) if runtime_agent is not None else None
    if service is not None:
        try:
            startup = getattr(service, "startup", None)
            if callable(startup):
                await startup()
        except Exception:
            logger.debug("main task service startup skipped during shutdown pause")
    store = getattr(service, "store", None) if service is not None else None

    paused_sessions = 0
    if current_manager is not None:
        for session_key in list(current_manager.list_sessions()):
            session = current_manager.get(session_key)
            if not SessionRuntimeBridge.session_is_running(session):
                continue
            try:
                await session.pause(manual=True)
            except Exception:
                logger.debug("session pause skipped during shutdown for {}", session_key)
                continue
            if store is not None and callable(getattr(store, "record_shutdown_pause_entry", None)):
                channel, chat_id = current_manager.session_meta(session_key) or ("", "")
                try:
                    store.record_shutdown_pause_entry(
                        kind="session",
                        ref_id=session_key,
                        channel=channel,
                        chat_id=chat_id,
                    )
                except Exception:
                    logger.debug("shutdown pause ledger write skipped for session {}", session_key)
            paused_sessions += 1

    paused_tasks = 0
    paused_task_ids: set[str] = set()
    if service is not None:
        for task in list(getattr(service.store, "list_tasks", lambda: [])() or []):
            status = str(getattr(task, "status", "") or "").strip().lower()
            if status != "in_progress" or bool(getattr(task, "is_paused", False)):
                continue
            task_id = str(getattr(task, "task_id", "") or "").strip()
            try:
                pause_impl = getattr(service, "force_pause_task_durably", None)
                if callable(pause_impl):
                    await pause_impl(task_id)
                else:
                    await service.pause_task(task_id)
            except Exception:
                logger.debug("task pause skipped during shutdown for {}", task_id)
                continue
            paused_task_ids.add(task_id)
            if store is not None and callable(getattr(store, "record_shutdown_pause_entry", None)):
                try:
                    store.record_shutdown_pause_entry(kind="task", ref_id=task_id)
                except Exception:
                    logger.debug("shutdown pause ledger write skipped for task {}", task_id)
            paused_tasks += 1
        if paused_task_ids:
            # The durable flags are set; wait for the worker to truly stop the
            # running actors (pause commands finished) before the process exits.
            await wait_shutdown_pause_commands_drained(service, task_ids=paused_task_ids)

    return {"paused_sessions": paused_sessions, "paused_tasks": paused_tasks}


async def resume_shutdown_paused_sessions(
    agent: AgentLoop | None = None,
    runtime_manager: SessionRuntimeManager | None = None,
    heartbeat: Any | None = None,
) -> int:
    """Auto-resume sessions that were paused by the previous graceful shutdown.

    Runs once after the heartbeat service is up: each ledger row wakes its
    session through the heartbeat internal-turn lane so the paused user
    request is reconciled back into the request seed and the model continues
    the interrupted work. Rows whose session no longer exists are retired.
    """
    runtime_agent = agent if agent is not None else _global_agent
    current_manager = runtime_manager if runtime_manager is not None else _global_runtime_manager
    heartbeat_service = heartbeat if heartbeat is not None else _global_web_heartbeat
    if runtime_agent is None or current_manager is None or heartbeat_service is None:
        return 0
    service = getattr(runtime_agent, "main_task_service", None)
    store = getattr(service, "store", None) if service is not None else None
    if store is None:
        return 0
    list_entries = getattr(store, "list_shutdown_pause_entries", None)
    consume = getattr(store, "mark_shutdown_pause_entry_consumed", None)
    enqueue = getattr(heartbeat_service, "enqueue_shutdown_resume", None)
    if not callable(list_entries) or not callable(consume) or not callable(enqueue):
        return 0
    session_manager = getattr(runtime_agent, "sessions", None)
    resumed = 0
    for entry in list_entries(kind="session"):
        session_key = str(entry.get("ref_id") or "").strip()
        if not session_key:
            consume(kind="session", ref_id=session_key)
            continue
        exists = False
        if session_manager is not None and callable(getattr(session_manager, "get_path", None)):
            try:
                exists = bool(getattr(session_manager, "get_path")(session_key).exists())
            except Exception:
                exists = False
        if not exists:
            consume(kind="session", ref_id=session_key)
            continue
        # Recreate the runtime session so its frontdoor continuity baseline is
        # restored before the wake turn runs.
        channel = str(entry.get("channel") or "").strip() or "web"
        chat_id = str(entry.get("chat_id") or "").strip() or session_key
        current_manager.get_or_create(session_key=session_key, channel=channel, chat_id=chat_id)
        if enqueue(session_key):
            resumed += 1
        consume(kind="session", ref_id=session_key)
    if resumed:
        logger.info("auto-resumed {} session(s) paused by shutdown", resumed)
    return resumed


async def replay_queued_follow_ups(
    agent: AgentLoop | None = None,
    runtime_manager: SessionRuntimeManager | None = None,
) -> int:
    """启动重放排队消息：进程重启后"已受理但从未成回合"的消息要在会话回到空闲时发出去。

    durable 记录是转录里的 pending 用户行，所以这里只做两件事：用有界尾窗粗筛出哪些会话
    值得构造（`keys_with_pending_user_rows`），然后把它们交给会话自己的派发器——构造期
    `_rehydrate_queued_follow_ups` 会把队列接回来，派发器再问一次 hold（可能刚被
    `resume_shutdown_paused_sessions` 唤醒成 running，那时它会让路）。

    逐个 await 而不是并发：刚起来的进程同时开 N 个回合会直接顶到 provider 限流上。
    返回真正发出消息的会话数。
    """
    runtime_agent = agent if agent is not None else _global_agent
    current_manager = runtime_manager if runtime_manager is not None else _global_runtime_manager
    if runtime_agent is None or current_manager is None:
        return 0
    session_manager = getattr(runtime_agent, "sessions", None)
    scan = getattr(session_manager, "keys_with_pending_user_rows", None)
    if not callable(scan):
        return 0
    try:
        keys = [str(item or "").strip() for item in list(scan() or []) if str(item or "").strip()]
    except Exception:
        logger.exception("queued follow-up boot scan skipped on error")
        return 0
    if not keys:
        return 0
    # 渠道/会话键的拆分不能想当然：naive split 会污染 runtime 会话元数据并让出站
    # 路由认错目标，因此复用 heartbeat 里已经处理过 ext:/china: 的那一份。
    from g3ku.heartbeat.session_service import _derive_session_channel_chat

    replayed = 0
    for session_key in keys:
        channel, chat_id = _derive_session_channel_chat(session_key)
        try:
            session = current_manager.get_or_create(
                session_key=session_key,
                channel=str(channel or "web"),
                chat_id=str(chat_id or "shared"),
            )
            dispatch = getattr(session, "dispatch_queued_follow_ups_if_idle", None)
            if not callable(dispatch):
                continue
            result = dict(await dispatch(source="boot_replay") or {})
        except Exception:
            logger.debug("queued follow-up boot replay skipped for {}", session_key)
            continue
        if int(result.get("dispatched") or 0) > 0:
            replayed += 1
    logger.warning(
        "queued follow-up boot replay: {} session(s) scanned, {} dispatched",
        len(keys),
        replayed,
    )
    return replayed


def get_web_heartbeat_service(agent: AgentLoop | None = None):
    runtime_agent = agent or get_agent()
    runtime_manager = get_runtime_manager(runtime_agent)
    global _global_web_heartbeat
    _global_web_heartbeat = build_web_session_heartbeat(
        runtime_agent,
        runtime_manager,
        reply_notifier=_make_heartbeat_reply_notifier(),
    )
    return _global_web_heartbeat


def describe_web_runtime_services(agent: AgentLoop | None = None) -> dict[str, bool]:
    runtime_agent = agent or _global_agent
    main_task_service = getattr(runtime_agent, 'main_task_service', None) if runtime_agent is not None else None
    heartbeat = _global_web_heartbeat
    main_runtime_ready = bool(main_task_service is not None and getattr(main_task_service, '_started', False))
    heartbeat_ready = bool(heartbeat is not None and getattr(heartbeat, '_started', False))
    bootstrapping = _get_runtime_services_lock().locked()
    ready = bool(runtime_agent is not None and main_runtime_ready and heartbeat_ready and not bootstrapping)
    return {
        'agent_ready': runtime_agent is not None,
        'main_runtime_ready': main_runtime_ready,
        'heartbeat_ready': heartbeat_ready,
        'bootstrapping': bootstrapping,
        'ready': ready,
    }


async def ensure_web_runtime_services(agent: AgentLoop | None = None) -> None:
    global _global_web_heartbeat
    if describe_web_runtime_services(agent).get('ready') and _cron_runtime_ready(agent):
        return

    async with _get_runtime_services_lock():
        runtime_agent = agent or get_agent()
        if describe_web_runtime_services(runtime_agent).get('ready') and _cron_runtime_ready(runtime_agent):
            return

        main_task_service = getattr(runtime_agent, 'main_task_service', None)
        if main_task_service is not None:
            await main_task_service.startup()
            # Avoid blocking unlock on worker warmup; the UI can surface worker readiness separately.
            await ensure_managed_task_worker(main_task_service, wait_timeout_s=1.0)
            _ensure_task_worker_watchdog_running(main_task_service)
        heartbeat = await start_web_session_heartbeat(
            runtime_agent,
            get_runtime_manager(runtime_agent),
            replay_pending_outbox=True,
            reply_notifier=_make_heartbeat_reply_notifier(),
        )
        if heartbeat is not None:
            _global_web_heartbeat = heartbeat
        cron_service = getattr(runtime_agent, "cron_service", None)
        if cron_service is not None and not _cron_runtime_ready(runtime_agent) and _should_start_web_cron(runtime_agent):
            await cron_service.start()
        _ensure_outbound_drain_running()
        await _replay_pending_external_outbox()
        _ensure_outbox_reconcile_running()
        try:
            await resume_shutdown_paused_sessions(runtime_agent, get_runtime_manager(runtime_agent), _global_web_heartbeat)
        except Exception:
            logger.debug("shutdown-paused session resume skipped during startup")
        try:
            await replay_queued_follow_ups(runtime_agent, get_runtime_manager(runtime_agent))
        except Exception:
            logger.debug("queued follow-up boot replay skipped during startup")
        await _sync_qq_official_service()


async def shutdown_web_runtime() -> None:
    global _global_agent, _global_bus, _global_runtime_manager, _global_web_heartbeat
    global _global_outbound_drain_task, _global_task_worker_watchdog_task, _global_qq_official_services
    global _global_outbox_reconcile_task

    agent = _global_agent
    runtime_manager = _global_runtime_manager
    heartbeat = _global_web_heartbeat
    cron_service = getattr(agent, "cron_service", None) if agent is not None else None
    outbound_drain_task = _global_outbound_drain_task
    task_worker_watchdog_task = _global_task_worker_watchdog_task
    outbox_reconcile_task = _global_outbox_reconcile_task
    qq_official_services = list(_global_qq_official_services.values())

    _global_agent = None
    _global_bus = None
    _global_runtime_manager = None
    _global_web_heartbeat = None
    _global_outbound_drain_task = None
    _global_task_worker_watchdog_task = None
    _global_outbox_reconcile_task = None
    _global_qq_official_services = {}

    if agent is None:
        return

    # Freeze all running work first (durably, with a startup-resume ledger row)
    # so no matter how this process exit was triggered, tasks and sessions
    # restart cleanly instead of being reported as abnormal stops.
    try:
        paused = await pause_running_work_for_shutdown(agent, runtime_manager)
        if paused.get("paused_sessions") or paused.get("paused_tasks"):
            logger.info(
                "graceful shutdown paused {} session(s) and {} task(s)",
                paused.get("paused_sessions", 0),
                paused.get("paused_tasks", 0),
            )
    except Exception:
        logger.debug("shutdown pause-all skipped during shutdown")

    if cron_service is not None:
        try:
            cron_service.stop()
        except Exception:
            logger.debug('web cron stop skipped during shutdown')

    if heartbeat is not None:
        try:
            await heartbeat.stop()
        except Exception:
            logger.debug('web heartbeat stop skipped during shutdown')

    await _cancel_background_task(outbound_drain_task)
    await _cancel_background_task(task_worker_watchdog_task)
    # 对账循环必须先于桥服务收割：循环每 5 轮会调 _sync_qq_official_service，
    # 顺序反了会出现「shutdown 停桥后对账又把桥拉起来」的复活竞态。
    await _cancel_background_task(outbox_reconcile_task)
    for service in qq_official_services:
        await service.stop()

    session_keys: set[str] = set()
    if runtime_manager is not None:
        try:
            session_keys.update(key for key in runtime_manager.list_sessions() if str(key or '').strip())
        except Exception:
            logger.debug('Runtime manager session enumeration skipped during shutdown')
    try:
        active_tasks = getattr(agent, '_active_tasks', None)
        if isinstance(active_tasks, dict):
            session_keys.update(key for key in active_tasks.keys() if str(key or '').strip())
    except Exception:
        logger.debug('Active session enumeration skipped during shutdown')

    for session_key in sorted(session_keys):
        try:
            await agent.cancel_session_tasks(session_key)
        except Exception:
            logger.debug('Session cancel skipped during shutdown for {}', session_key)

    pool = getattr(agent, 'background_pool', None)
    if pool is not None and hasattr(pool, 'close'):
        try:
            await pool.close()
        except Exception:
            logger.debug('Background pool close skipped during shutdown')

    main_task_service = getattr(agent, 'main_task_service', None)
    if main_task_service is not None:
        try:
            await main_task_service.close()
        except Exception:
            logger.debug('main task service close skipped during shutdown')

    if main_task_service is not None and not keep_worker_enabled() and managed_worker_pid() is not None:
        # The managed worker is about to be killed without running its own
        # close(); drop its lease now so an immediate restart does not collide
        # with the stale-lease window (lease rows outlive the worker by TTL).
        try:
            main_task_service.release_task_worker_lease_durably()
        except Exception:
            logger.debug('task worker lease release skipped during shutdown')

    try:
        await shutdown_managed_task_worker()
    except Exception:
        logger.debug('managed task worker stop skipped during shutdown')

    try:
        await agent.close_mcp()
    except Exception:
        logger.debug('Agent runtime close skipped during shutdown')


def run_web_shell(*, host: str | None, port: int | None, reload: bool, debug: bool, set_debug_mode, with_worker: bool = True) -> None:
    set_debug_mode(debug)
    run_web_server_entrypoint(
        host=host,
        port=port,
        reload=reload,
        log_level='debug' if debug else 'info',
        with_worker=with_worker,
    )


__all__ = [
    'describe_web_runtime_services',
    'debug_trace_enabled',
    'ensure_web_runtime_services',
    'get_agent',
    'is_no_ceo_model_configured_error',
    'get_runtime_manager',
    'get_web_heartbeat_service',
    'no_ceo_model_configured_payload',
    'pause_running_work_for_shutdown',
    'refresh_web_agent_runtime',
    'resume_shutdown_paused_sessions',
    'run_web_shell',
    'shutdown_web_runtime',
    'wait_shutdown_pause_commands_drained',
]
