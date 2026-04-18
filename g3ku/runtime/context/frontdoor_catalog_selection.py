from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from g3ku.agent.catalog_store import DashScopeTextReranker
from g3ku.config.live_runtime import get_runtime_config
from g3ku.llm_config.runtime_resolver import resolve_memory_rerank_target
from g3ku.providers.chatmodels import build_chat_model
from g3ku.runtime.context.frontdoor_query_rewriter import (
    REWRITE_PROMPT_REVISION,
    FrontdoorRewriteResult,
    build_query_rewrite_cache_key,
    build_query_rewrite_exposure_revision,
    build_query_rewrite_runtime_identity,
    canonicalize_visible_ids,
)


CATALOG_NAMESPACE: tuple[str, ...] = ("catalog", "global")
_FRONTDOOR_QUERY_REWRITE_CACHE: dict[str, FrontdoorRewriteResult] = {}


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _normalized_text(value: Any) -> str:
    return str(value or "").strip()


def _frontdoor_query_rewrite_enabled() -> bool:
    raw = _normalized_text(os.getenv("G3KU_ENABLE_FRONTDOOR_QUERY_REWRITE"))
    if not raw:
        return True
    return raw.lower() not in {"0", "false", "no", "off"}


def _visible_ids(items: list[Any], *, key: str) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(items or []):
        value = _normalized_text(_item_value(item, key))
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _record_id(record: Any) -> str:
    return _normalized_text(_item_value(record, "record_id"))


def _catalog_record_suffix(record_id: str, *, prefix: str) -> str:
    text = _normalized_text(record_id)
    if not text.startswith(prefix):
        return ""
    return text[len(prefix) :].strip()


def _resource_record_executor_name(record: Any) -> str:
    record_id = _record_id(record)
    suffix = _catalog_record_suffix(record_id, prefix="tool:")
    if not suffix:
        return ""
    return suffix.split("::", 1)[-1].strip()


def _family_executor_names(family: Any) -> list[str]:
    executor_names: list[str] = []
    for action in list(_item_value(family, "actions") or []):
        for raw_name in list(_item_value(action, "executor_names") or []):
            name = _normalized_text(raw_name)
            if name and name not in executor_names:
                executor_names.append(name)
    tool_id = _normalized_text(_item_value(family, "tool_id"))
    if not executor_names and tool_id:
        executor_names.append(tool_id)
    return executor_names


def _visible_tool_executor_names(visible_families: list[Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for family in list(visible_families or []):
        for executor_name in _family_executor_names(family):
            if executor_name in seen:
                continue
            seen.add(executor_name)
            ordered.append(executor_name)
    return ordered


def _executor_family_map(visible_families: list[Any]) -> dict[str, str]:
    family_map: dict[str, str] = {}
    for family in list(visible_families or []):
        tool_id = _normalized_text(_item_value(family, "tool_id"))
        if not tool_id:
            continue
        for executor_name in _family_executor_names(family):
            family_map.setdefault(executor_name, tool_id)
    return family_map


def _record_text(record: Any) -> str:
    for key in ("l1", "l0", "text", "content", "description", "display_name", "record_id"):
        value = _normalized_text(_item_value(record, key))
        if value:
            return value
    return ""


def _split_provider_model(raw: str, *, default_provider: str | None = None) -> tuple[str | None, str]:
    text = _normalized_text(raw)
    if ":" in text:
        provider, model = text.split(":", 1)
        return _normalized_text(provider).lower() or None, _normalized_text(model)
    provider = _normalized_text(default_provider).lower()
    return provider or None, text


def _is_dashscope_rerank_model(model: str) -> bool:
    provider, model_id = _split_provider_model(model, default_provider="dashscope")
    return model_id == "qwen3-vl-rerank" and provider in {None, "dashscope"}


def _visible_dense_hits(
    records: list[Any],
    *,
    visible_ids: set[str],
    prefix: str,
) -> tuple[list[Any], dict[str, int]]:
    filtered: list[Any] = []
    dense_rank_by_record_id: dict[str, int] = {}
    seen: set[str] = set()
    for dense_rank, record in enumerate(list(records or []), start=1):
        record_id = _record_id(record)
        entity_id = _catalog_record_suffix(record_id, prefix=prefix)
        if not entity_id or entity_id not in visible_ids or entity_id in seen:
            continue
        seen.add(entity_id)
        filtered.append(record)
        dense_rank_by_record_id[record_id] = dense_rank
    return filtered, dense_rank_by_record_id


def _selected_catalog_ids(
    records: list[Any],
    *,
    limit: int,
    prefix: str,
    id_key: str,
    dense_rank_by_record_id: dict[str, int],
) -> tuple[list[str], list[dict[str, Any]]]:
    selected_ids: list[str] = []
    trace: list[dict[str, Any]] = []
    seen: set[str] = set()
    capped_limit = max(int(limit or 0), 0)
    for rerank_rank, record in enumerate(list(records or []), start=1):
        record_id = _record_id(record)
        entity_id = _catalog_record_suffix(record_id, prefix=prefix)
        if not entity_id or entity_id in seen:
            continue
        seen.add(entity_id)
        selected_ids.append(entity_id)
        trace.append(
            {
                "record_id": record_id,
                id_key: entity_id,
                "dense_rank": dense_rank_by_record_id.get(record_id),
                "rerank_rank": rerank_rank,
            }
        )
        if capped_limit and len(selected_ids) >= capped_limit:
            break
    return selected_ids, trace


def _selected_executor_ids(
    records: list[Any],
    *,
    limit: int,
    executor_family_map: dict[str, str],
    dense_rank_by_record_id: dict[str, int],
) -> tuple[list[str], list[dict[str, Any]]]:
    selected_ids: list[str] = []
    trace: list[dict[str, Any]] = []
    seen: set[str] = set()
    capped_limit = max(int(limit or 0), 0)
    for rerank_rank, record in enumerate(list(records or []), start=1):
        record_id = _record_id(record)
        executor_name = _resource_record_executor_name(record)
        if not executor_name or executor_name in seen:
            continue
        seen.add(executor_name)
        selected_ids.append(executor_name)
        trace.append(
            {
                "record_id": record_id,
                "tool_id": executor_name,
                "executor_name": executor_name,
                "family_id": str(executor_family_map.get(executor_name) or ""),
                "dense_rank": dense_rank_by_record_id.get(record_id),
                "rerank_rank": rerank_rank,
            }
        )
        if capped_limit and len(selected_ids) >= capped_limit:
            break
    return selected_ids, trace


def _dense_trace(
    records: list[Any],
    *,
    prefix: str,
    id_key: str,
    dense_rank_by_record_id: dict[str, int],
) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for record in list(records or []):
        record_id = _record_id(record)
        entity_id = _catalog_record_suffix(record_id, prefix=prefix)
        if not entity_id:
            continue
        trace.append(
            {
                "record_id": record_id,
                id_key: entity_id,
                "dense_rank": dense_rank_by_record_id.get(record_id),
            }
        )
    return trace


def _dense_executor_trace(
    records: list[Any],
    *,
    executor_family_map: dict[str, str],
    dense_rank_by_record_id: dict[str, int],
) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for record in list(records or []):
        record_id = _record_id(record)
        executor_name = _resource_record_executor_name(record)
        if not executor_name:
            continue
        trace.append(
            {
                "record_id": record_id,
                "tool_id": executor_name,
                "executor_name": executor_name,
                "family_id": str(executor_family_map.get(executor_name) or ""),
                "dense_rank": dense_rank_by_record_id.get(record_id),
            }
        )
    return trace


def _filter_rerank_scores(
    scores: list[dict[str, Any]],
    *,
    prefix: str,
    visible_ids: set[str],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for item in list(scores or []):
        record_id = _normalized_text(_item_value(item, "record_id"))
        entity_id = _catalog_record_suffix(record_id, prefix=prefix)
        if not entity_id or entity_id not in visible_ids:
            continue
        filtered.append(
            {
                "record_id": record_id,
                "score": _item_value(item, "score"),
                "rerank_rank": _item_value(item, "rerank_rank"),
            }
        )
    return filtered


def _default_rerank_trace(*, status: str, model: str, top_n: int) -> dict[str, Any]:
    return {
        "status": _normalized_text(status),
        "model": _normalized_text(model),
        "top_n": max(int(top_n or 1), 1),
        "scores": [],
    }


def _selection_payload(*, mode: str, available: bool) -> dict[str, Any]:
    return {
        "mode": _normalized_text(mode),
        "available": bool(available),
        "skill_ids": [],
        "tool_ids": [],
        "trace": {
            "queries": {},
            "dense": {"skills": [], "tools": []},
            "rerank": {"skills": {}, "tools": {}},
        },
    }


def _response_text(value: Any) -> str:
    raw = getattr(value, "content", value)
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(raw or "").strip()


def _extract_json_dict(text: str) -> dict[str, Any]:
    raw = _normalized_text(text)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            payload = json.loads(raw[start : end + 1])
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}


def _truncate_chars(text: str, limit: int = 240) -> str:
    value = _normalized_text(text)
    if len(value) <= max(limit, 1):
        return value
    return value[: max(limit - 3, 0)].rstrip() + "..."


def _query_contains_any(query_text: str, patterns: tuple[str, ...]) -> bool:
    raw = _normalized_text(query_text)
    lower = raw.lower()
    for pattern in patterns:
        token = _normalized_text(pattern)
        if not token:
            continue
        if token.isascii():
            if token.lower() in lower:
                return True
            continue
        if token in raw:
            return True
    return False


def _filesystem_intent_targets(raw_query: str, visible_ids: list[str]) -> list[str]:
    visible = {
        _normalized_text(item)
        for item in list(visible_ids or [])
        if _normalized_text(item)
    }
    if not visible:
        return []

    ordered_targets: list[str] = []

    def _append_if_visible(*tool_ids: str) -> None:
        for tool_id in tool_ids:
            normalized = _normalized_text(tool_id)
            if normalized and normalized in visible and normalized not in ordered_targets:
                ordered_targets.append(normalized)

    if _query_contains_any(raw_query, ("patch", "diff", "propose patch", "补丁", "差异")):
        _append_if_visible("filesystem_propose_patch")
    if _query_contains_any(raw_query, ("delete", "remove", "cleanup", "删除", "移除", "清理")):
        _append_if_visible("filesystem_delete")
    if _query_contains_any(raw_query, ("move", "rename", "relocate", "移动", "重命名")):
        _append_if_visible("filesystem_move")
    if _query_contains_any(raw_query, ("copy", "duplicate", "复制", "拷贝")):
        _append_if_visible("filesystem_copy")
    if _query_contains_any(
        raw_query,
        (
            "append",
            "prepend",
            "insert",
            "replace",
            "modify",
            "update",
            "edit",
            "change",
            "line",
            "append a line",
            "insert a line",
            "修改",
            "更新",
            "编辑",
            "追加",
            "插入",
            "替换",
            "改写",
            "行",
        ),
    ):
        _append_if_visible("filesystem_edit")
    if _query_contains_any(
        raw_query,
        (
            "write",
            "create",
            "new file",
            "generate file",
            "save file",
            "markdown file",
            "写入",
            "创建",
            "新建",
            "生成文件",
            "保存文件",
        ),
    ):
        _append_if_visible("filesystem_write")

    return ordered_targets


def _compose_rewritten_query(
    *,
    raw_query: str,
    kind: str,
    visible_ids: list[str],
) -> str:
    query = _normalized_text(raw_query)
    if not query:
        return ""
    focus_label = "visible skills/workflows" if kind == "skill" else "visible tools/resources"
    focus_ids = ", ".join(visible_ids[:6])
    kind_hint = "skill workflow capability selection" if kind == "skill" else "tool resource executor selection"
    if kind == "tool":
        concrete_filesystem_targets = _filesystem_intent_targets(query, visible_ids)
        if concrete_filesystem_targets:
            target_text = ", ".join(concrete_filesystem_targets[:4])
            if focus_ids:
                return (
                    f"{query}; prioritize {kind_hint}; concrete filesystem executors: {target_text}; "
                    f"{focus_label}: {focus_ids}"
                )
            return f"{query}; prioritize {kind_hint}; concrete filesystem executors: {target_text}"
    if focus_ids:
        return f"{query}; prioritize {kind_hint}; {focus_label}: {focus_ids}"
    return f"{query}; prioritize {kind_hint}"


async def _invoke_frontdoor_catalog_rewrite_model(
    *,
    loop: Any,
    memory_manager: Any | None,
    query_text: str | None = None,
    visible_skill_ids: list[str] | None = None,
    visible_tool_ids: list[str] | None = None,
    exposure_revision: str | None = None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ = loop, memory_manager
    config, _revision, _changed = get_runtime_config(force=False)
    model_key = str(config.resolve_role_model_key("ceo") or "").strip()
    model = build_chat_model(config, role="ceo")
    effective_request = dict(request or {})
    if not effective_request:
        effective_request = _build_frontdoor_query_rewrite_request(
            raw_query=_normalized_text(query_text),
            visible_skill_ids=list(visible_skill_ids or []),
            visible_tool_ids=list(visible_tool_ids or []),
        )
    if exposure_revision is not None and _normalized_text(exposure_revision):
        effective_request["exposure_revision"] = _normalized_text(exposure_revision)
    prompt = (
        "You rewrite internal retrieval queries for a frontdoor catalog selector.\n"
        "Return strict JSON only with keys skill_query and tool_query.\n"
        "Each query must stay short, retrieval-oriented, and specialized for its target.\n"
        "skill_query should target visible skills/workflows only.\n"
        "tool_query should target visible tools/resources only.\n"
        "Use the user query and visible inventory hints. Do not mention JSON or explanations."
    )
    body = json.dumps(
        {
            "raw_query": _truncate_chars(_normalized_text(effective_request.get("raw_query")), 220),
            "visible_skill_ids": list(effective_request.get("visible_skill_ids") or [])[:12],
            "visible_tool_ids": list(effective_request.get("visible_tool_ids") or [])[:12],
            "exposure_revision": _normalized_text(effective_request.get("exposure_revision")),
        },
        ensure_ascii=False,
    )
    response = await model.ainvoke(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": body},
        ]
    )
    parsed = _extract_json_dict(_response_text(response))
    return {
        "skill_query": _truncate_chars(str(parsed.get("skill_query") or "")),
        "tool_query": _truncate_chars(str(parsed.get("tool_query") or "")),
        "model": model_key,
    }


def _query_rewrite_cache_for(memory_manager: Any | None) -> dict[str, FrontdoorRewriteResult]:
    if memory_manager is None:
        return _FRONTDOOR_QUERY_REWRITE_CACHE
    current = getattr(memory_manager, "_frontdoor_query_rewrite_cache", None)
    if isinstance(current, dict):
        return current
    try:
        cache: dict[str, FrontdoorRewriteResult] = {}
        setattr(memory_manager, "_frontdoor_query_rewrite_cache", cache)
        return cache
    except Exception:
        return _FRONTDOOR_QUERY_REWRITE_CACHE


def _resolve_query_rewrite_runtime_identity() -> str:
    try:
        config, revision, _changed = get_runtime_config(force=False)
    except Exception:
        return build_query_rewrite_runtime_identity(model_key="", runtime_revision="")
    model_key = ""
    try:
        model_key = str(config.resolve_role_model_key("ceo") or "").strip()
    except Exception:
        model_key = ""
    return build_query_rewrite_runtime_identity(
        model_key=model_key,
        runtime_revision=revision,
    )


def _build_frontdoor_query_rewrite_request(
    *,
    raw_query: str,
    visible_skill_ids: list[str],
    visible_tool_ids: list[str],
) -> dict[str, Any]:
    canonical_skill_ids = canonicalize_visible_ids(visible_skill_ids)
    canonical_tool_ids = canonicalize_visible_ids(visible_tool_ids)
    return {
        "raw_query": _normalized_text(raw_query),
        "visible_skill_ids": list(canonical_skill_ids),
        "visible_tool_ids": list(canonical_tool_ids),
        "exposure_revision": build_query_rewrite_exposure_revision(
            visible_skill_ids=list(canonical_skill_ids),
            visible_tool_ids=list(canonical_tool_ids),
        ),
    }


def _rewrite_result_to_public_dict(result: FrontdoorRewriteResult | dict[str, Any]) -> dict[str, str]:
    if isinstance(result, dict):
        return {
            "raw_query": _normalized_text(result.get("raw_query")),
            "skill_query": _normalized_text(result.get("skill_query")),
            "tool_query": _normalized_text(result.get("tool_query")),
            "status": _normalized_text(result.get("status")),
            "model": _normalized_text(result.get("model")),
        }
    return {
        "raw_query": _normalized_text(result.raw_query),
        "skill_query": _normalized_text(result.skill_query),
        "tool_query": _normalized_text(result.tool_query),
        "status": _normalized_text(result.status),
        "model": _normalized_text(result.model),
    }


async def _rewrite_frontdoor_catalog_queries_sidecar(
    *,
    loop: Any,
    memory_manager: Any | None,
    query_text: str,
    visible_skills: list[Any],
    visible_families: list[Any],
) -> FrontdoorRewriteResult:
    query = _normalized_text(query_text)
    skill_ids = _visible_ids(visible_skills, key="skill_id")
    tool_ids = _visible_tool_executor_names(visible_families)
    request = _build_frontdoor_query_rewrite_request(
        raw_query=query,
        visible_skill_ids=skill_ids,
        visible_tool_ids=tool_ids,
    )
    exposure_revision = _normalized_text(request.get("exposure_revision"))
    cache_key = build_query_rewrite_cache_key(
        raw_query=query,
        exposure_revision=exposure_revision,
        rewrite_prompt_revision=REWRITE_PROMPT_REVISION,
    )
    runtime_identity = _resolve_query_rewrite_runtime_identity()
    scoped_cache_key = f"{runtime_identity}:{cache_key}"
    rewrite_cache = _query_rewrite_cache_for(memory_manager)
    dense_enabled = bool(getattr(getattr(memory_manager, "store", None), "_dense_enabled", False))

    fallback_skill_query = _compose_rewritten_query(raw_query=query, kind="skill", visible_ids=skill_ids) or query
    fallback_tool_query = _compose_rewritten_query(raw_query=query, kind="tool", visible_ids=tool_ids) or query

    if not query:
        return FrontdoorRewriteResult(
            raw_query="",
            skill_query="",
            tool_query="",
            status="empty",
            model="",
            exposure_revision=exposure_revision,
            cache_key=cache_key,
        )

    if not _frontdoor_query_rewrite_enabled():
        return FrontdoorRewriteResult(
            raw_query=query,
            skill_query=query,
            tool_query=query,
            status="passthrough",
            model="",
            exposure_revision=exposure_revision,
            cache_key=cache_key,
        )

    cached = rewrite_cache.get(scoped_cache_key)
    if cached is not None:
        return cached

    if dense_enabled and (skill_ids or tool_ids):
        try:
            model_result = await _invoke_frontdoor_catalog_rewrite_model(
                loop=loop,
                memory_manager=memory_manager,
                query_text=query,
                visible_skill_ids=skill_ids,
                visible_tool_ids=tool_ids,
                exposure_revision=exposure_revision,
                request=request,
            )
        except Exception:
            model_result = {}
        raw_skill_query = _normalized_text((model_result or {}).get("skill_query"))
        raw_tool_query = _normalized_text((model_result or {}).get("tool_query"))
        model = _normalized_text((model_result or {}).get("model"))
        skill_query = raw_skill_query or fallback_skill_query
        tool_query = raw_tool_query or fallback_tool_query
        if model and (raw_skill_query or raw_tool_query):
            status = "rewritten"
        else:
            status = "fallback"
            skill_query = fallback_skill_query
            tool_query = fallback_tool_query
            model = ""
    else:
        status = "passthrough"
        skill_query = query
        tool_query = query
        model = ""

    result = FrontdoorRewriteResult(
        raw_query=query,
        skill_query=skill_query,
        tool_query=tool_query,
        status=status,
        model=model,
        exposure_revision=exposure_revision,
        cache_key=cache_key,
    )
    if result.status == "rewritten":
        rewrite_cache[scoped_cache_key] = result
    return result


def _resolve_frontdoor_catalog_reranker(memory_manager: Any | None) -> tuple[Any | None, str, str]:
    workspace = getattr(memory_manager, "workspace", None) if memory_manager is not None else None
    if not workspace:
        return None, "", "unconfigured"

    try:
        target = resolve_memory_rerank_target(workspace=Path(workspace).expanduser().resolve())
    except Exception:
        return None, "", "unconfigured"

    model = _normalized_text(getattr(target, "resolved_model", ""))
    if not model:
        return None, "", "unconfigured"
    if not _is_dashscope_rerank_model(model):
        return None, model, "unsupported"

    _, model_id = _split_provider_model(model, default_provider="dashscope")
    secret_payload = getattr(target, "secret_payload", {}) or {}
    api_key = _normalized_text(secret_payload.get("api_key")) or _normalized_text(os.environ.get("DASHSCOPE_API_KEY"))
    if not api_key:
        return None, model, "missing_api_key"

    return (
        DashScopeTextReranker(
            api_key=api_key,
            model=model_id,
            api_base=_normalized_text(getattr(target, "base_url", "")) or None,
        ),
        model,
        "configured",
    )


async def rewrite_frontdoor_catalog_queries(
    *,
    loop: Any,
    memory_manager: Any | None,
    query_text: str,
    visible_skills: list[Any],
    visible_families: list[Any],
) -> dict[str, str]:
    return _rewrite_result_to_public_dict(
        await _rewrite_frontdoor_catalog_queries_sidecar(
            loop=loop,
            memory_manager=memory_manager,
            query_text=query_text,
            visible_skills=visible_skills,
            visible_families=visible_families,
        )
    )


async def rerank_frontdoor_catalog_records(
    *,
    memory_manager: Any | None,
    query_text: str,
    records: list[Any],
    top_n: int,
) -> dict[str, Any]:
    ordered = list(records or [])
    if not ordered:
        return {"records": [], "trace": _default_rerank_trace(status="empty", model="", top_n=top_n)}

    reranker, model, status = _resolve_frontdoor_catalog_reranker(memory_manager)
    if reranker is None or not hasattr(reranker, "rerank"):
        return {
            "records": ordered,
            "trace": _default_rerank_trace(status=status, model=model, top_n=top_n),
        }

    documents = [_record_text(record)[:2000] for record in ordered]
    if not any(documents):
        return {
            "records": ordered,
            "trace": _default_rerank_trace(status="no_documents", model=model, top_n=top_n),
        }

    try:
        ranked = await asyncio.to_thread(
            reranker.rerank,
            query=_normalized_text(query_text),
            documents=documents,
            top_n=max(int(top_n or 1), 1),
        )
    except Exception:
        return {
            "records": ordered,
            "trace": _default_rerank_trace(status="error", model=model, top_n=top_n),
        }

    reranked: list[Any] = []
    seen: set[int] = set()
    score_trace: list[dict[str, Any]] = []
    for index, _score in list(ranked or []):
        if not isinstance(index, int) or index < 0 or index >= len(ordered) or index in seen:
            continue
        seen.add(index)
        reranked.append(ordered[index])
        if len(score_trace) < max(int(top_n or 1), 1):
            score_trace.append(
                {
                    "record_id": _record_id(ordered[index]),
                    "score": _score,
                    "rerank_rank": len(score_trace) + 1,
                }
            )
    if len(reranked) < len(ordered):
        for idx, record in enumerate(ordered):
            if idx in seen:
                continue
            reranked.append(record)
    return {
        "records": reranked or ordered,
        "trace": {
            "status": "configured",
            "model": model,
            "top_n": max(int(top_n or 1), 1),
            "scores": score_trace,
        },
    }


async def build_frontdoor_catalog_selection(
    *,
    loop: Any,
    memory_manager: Any | None,
    query_text: str,
    visible_skills: list[Any],
    visible_families: list[Any],
    skill_limit: int,
    tool_limit: int,
) -> dict[str, Any]:
    query = _normalized_text(query_text)
    if not query:
        return _selection_payload(mode="disabled", available=False)
    if memory_manager is None or not hasattr(memory_manager, "semantic_search_context_records"):
        return _selection_payload(mode="disabled", available=False)
    if not bool(getattr(getattr(memory_manager, "store", None), "_dense_enabled", False)):
        return _selection_payload(mode="unavailable", available=False)

    try:
        rewrites = await rewrite_frontdoor_catalog_queries(
            loop=loop,
            memory_manager=memory_manager,
            query_text=query,
            visible_skills=visible_skills,
            visible_families=visible_families,
        )
    except Exception:
        rewrites = {
            "raw_query": query,
            "skill_query": query,
            "tool_query": query,
            "status": "error",
            "model": "",
        }
    rewrite_public = _rewrite_result_to_public_dict(rewrites)
    query_trace = {
        "raw_query": _normalized_text(rewrite_public.get("raw_query")) or query,
        "skill_query": _normalized_text(rewrite_public.get("skill_query")) or query,
        "tool_query": _normalized_text(rewrite_public.get("tool_query")) or query,
        "status": _normalized_text(rewrite_public.get("status")) or ("passthrough" if query else "empty"),
        "model": _normalized_text(rewrite_public.get("model")),
    }
    skill_query = query_trace["skill_query"]
    tool_query = query_trace["tool_query"]
    skill_visible_ids = set(_visible_ids(visible_skills, key="skill_id"))
    tool_visible_ids = set(_visible_tool_executor_names(visible_families))
    executor_family_map = _executor_family_map(visible_families)

    async def _search(*, search_query: str, limit: int, context_type: str) -> list[Any]:
        if (
            memory_manager is None
            or not hasattr(memory_manager, "semantic_search_context_records")
            or not search_query
        ):
            return []
        try:
            return list(
                await memory_manager.semantic_search_context_records(
                    namespace_prefix=CATALOG_NAMESPACE,
                    query=search_query,
                    limit=max(int(limit or 1), 1),
                    context_type=context_type,
                )
                or []
            )
        except Exception:
            return []

    skill_dense_hits = await _search(search_query=skill_query, limit=skill_limit, context_type="skill")
    tool_dense_hits = await _search(search_query=tool_query, limit=tool_limit, context_type="resource")

    visible_skill_hits, skill_dense_rank = _visible_dense_hits(
        skill_dense_hits,
        visible_ids=skill_visible_ids,
        prefix="skill:",
    )
    visible_tool_hits, tool_dense_rank = _visible_dense_hits(
        tool_dense_hits,
        visible_ids=tool_visible_ids,
        prefix="tool:",
    )

    try:
        skill_rerank = await rerank_frontdoor_catalog_records(
            memory_manager=memory_manager,
            query_text=skill_query,
            records=visible_skill_hits,
            top_n=max(int(skill_limit or 1), 1),
        )
    except Exception:
        skill_rerank = {
            "records": list(visible_skill_hits),
            "trace": _default_rerank_trace(status="error", model="", top_n=skill_limit),
        }
    try:
        tool_rerank = await rerank_frontdoor_catalog_records(
            memory_manager=memory_manager,
            query_text=tool_query,
            records=visible_tool_hits,
            top_n=max(int(tool_limit or 1), 1),
        )
    except Exception:
        tool_rerank = {
            "records": list(visible_tool_hits),
            "trace": _default_rerank_trace(status="error", model="", top_n=tool_limit),
        }
    reranked_skill_hits = list((skill_rerank or {}).get("records") or [])
    reranked_tool_hits = list((tool_rerank or {}).get("records") or [])

    skill_ids, skill_trace = _selected_catalog_ids(
        reranked_skill_hits,
        limit=skill_limit,
        prefix="skill:",
        id_key="skill_id",
        dense_rank_by_record_id=skill_dense_rank,
    )
    tool_ids, tool_trace = _selected_executor_ids(
        reranked_tool_hits,
        limit=tool_limit,
        executor_family_map=executor_family_map,
        dense_rank_by_record_id=tool_dense_rank,
    )

    return {
        **_selection_payload(mode="dense_only", available=True),
        "skill_ids": skill_ids,
        "tool_ids": tool_ids,
        "trace": {
            "queries": query_trace,
            "dense": {
                "skills": _dense_trace(
                    visible_skill_hits,
                    prefix="skill:",
                    id_key="skill_id",
                    dense_rank_by_record_id=skill_dense_rank,
                ),
                "tools": _dense_executor_trace(
                    visible_tool_hits,
                    executor_family_map=executor_family_map,
                    dense_rank_by_record_id=tool_dense_rank,
                ),
            },
            "rerank": {
                "skills": {
                    **dict((skill_rerank or {}).get("trace") or {}),
                    "scores": _filter_rerank_scores(
                        list(dict((skill_rerank or {}).get("trace") or {}).get("scores") or []),
                        prefix="skill:",
                        visible_ids=skill_visible_ids,
                    ),
                    "selected": list(skill_trace),
                },
                "tools": {
                    **dict((tool_rerank or {}).get("trace") or {}),
                    "scores": _filter_rerank_scores(
                        list(dict((tool_rerank or {}).get("trace") or {}).get("scores") or []),
                        prefix="tool:",
                        visible_ids=tool_visible_ids,
                    ),
                    "selected": list(tool_trace),
                },
            },
        },
    }
