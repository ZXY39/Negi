from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .ceo_runner import CeoFrontDoorRunner


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dict__"):
        payload = dict(vars(value))
        if not payload:
            return str(value)
        return {
            str(key): _json_safe(item)
            for key, item in payload.items()
        }
    return str(value)


def _thread_config(*, session_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    configurable = {"thread_id": str(session_id or "").strip()}
    normalized_checkpoint_id = str(checkpoint_id or "").strip()
    if normalized_checkpoint_id:
        configurable["checkpoint_id"] = normalized_checkpoint_id
    return {"configurable": configurable}


def _serialize_interrupt(item: Any) -> dict[str, Any]:
    return {
        "id": str(getattr(item, "id", "") or ""),
        "value": _json_safe(getattr(item, "value", None)),
    }


def _serialize_task(item: Any) -> dict[str, Any]:
    interrupts = [_serialize_interrupt(interrupt_item) for interrupt_item in list(getattr(item, "interrupts", ()) or ())]
    payload = {
        "id": str(getattr(item, "id", "") or ""),
        "name": str(getattr(item, "name", "") or ""),
        "error": str(getattr(item, "error", "") or ""),
        "interrupts": interrupts,
    }
    state = getattr(item, "state", None)
    if state is not None:
        payload["state"] = _json_safe(state)
    return payload


def serialize_state_snapshot(snapshot: Any) -> dict[str, Any]:
    config = dict(getattr(snapshot, "config", {}) or {})
    configurable = dict(config.get("configurable", {}) or {})
    parent_config = dict(getattr(snapshot, "parent_config", {}) or {})
    parent_configurable = dict(parent_config.get("configurable", {}) or {})
    tasks = [_serialize_task(task) for task in list(getattr(snapshot, "tasks", ()) or ())]
    return {
        "thread_id": str(configurable.get("thread_id") or ""),
        "checkpoint_id": str(configurable.get("checkpoint_id") or ""),
        "checkpoint_ns": str(configurable.get("checkpoint_ns") or ""),
        "parent_checkpoint_id": str(parent_configurable.get("checkpoint_id") or ""),
        "values": _json_safe(dict(getattr(snapshot, "values", {}) or {})),
        "next": _json_safe(list(getattr(snapshot, "next", ()) or ())),
        "metadata": _json_safe(dict(getattr(snapshot, "metadata", {}) or {})),
        "created_at": str(getattr(snapshot, "created_at", "") or ""),
        "tasks": tasks,
        "has_interrupts": any(bool(task.get("interrupts")) for task in tasks),
    }


async def _runner_for_loop(loop) -> CeoFrontDoorRunner | Any:
    runner = getattr(loop, "multi_agent_runner", None)
    if runner is None:
        runner_cls = globals().get("CeoFrontDoorRunner")
        if runner_cls is None:
            from .ceo_runner import CeoFrontDoorRunner as runner_cls

        runner = runner_cls(loop=loop)
    ensure_ready = getattr(loop, "_ensure_checkpointer_ready", None)
    if callable(ensure_ready):
        result = ensure_ready()
        if hasattr(result, "__await__"):
            await result
    return runner


async def get_frontdoor_checkpoint(
    loop,
    *,
    session_id: str,
    checkpoint_id: str | None = None,
    subgraphs: bool = False,
) -> dict[str, Any] | None:
    runner = await _runner_for_loop(loop)
    snapshot = await runner._get_compiled_graph().aget_state(
        _thread_config(session_id=session_id, checkpoint_id=checkpoint_id),
        subgraphs=subgraphs,
    )
    return None if snapshot is None else serialize_state_snapshot(snapshot)


async def get_frontdoor_checkpoint_history(
    loop,
    *,
    session_id: str,
    limit: int = 20,
    before_checkpoint_id: str | None = None,
    metadata_filter: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    runner = await _runner_for_loop(loop)
    normalized_limit = 20 if limit is None else max(1, int(limit))
    before = (
        _thread_config(session_id=session_id, checkpoint_id=before_checkpoint_id)
        if str(before_checkpoint_id or "").strip()
        else None
    )
    items: list[dict[str, Any]] = []
    async for snapshot in runner._get_compiled_graph().aget_state_history(
        _thread_config(session_id=session_id),
        filter=dict(metadata_filter or {}) or None,
        before=before,
        limit=normalized_limit,
    ):
        items.append(serialize_state_snapshot(snapshot))
    return items


def build_frontdoor_replay_diagnostics(snapshot: dict[str, Any]) -> dict[str, Any]:
    item = dict(snapshot or {})
    metadata = dict(item.get("metadata") or {})
    values = dict(item.get("values") or {})
    prompt_cache_diagnostics = _json_safe(dict(values.get("prompt_cache_diagnostics") or {}))
    configurable = {
        "thread_id": str(item.get("thread_id") or ""),
    }
    checkpoint_id = str(item.get("checkpoint_id") or "")
    checkpoint_ns = str(item.get("checkpoint_ns") or "")
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    if checkpoint_ns:
        configurable["checkpoint_ns"] = checkpoint_ns
    result = {
        "thread_id": str(item.get("thread_id") or ""),
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": str(item.get("parent_checkpoint_id") or ""),
        "step": int(metadata.get("step") or 0),
        "source": str(metadata.get("source") or ""),
        "next": list(item.get("next") or []),
        "has_interrupts": bool(item.get("has_interrupts")),
        "replay_config": {
            "configurable": configurable
        },
    }
    if prompt_cache_diagnostics:
        result["prompt_cache_diagnostics"] = prompt_cache_diagnostics
    return result
