from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any

from loguru import logger

from g3ku.agent.tools.base import Tool
from g3ku.content import parse_content_envelope
from g3ku.json_schema_utils import normalize_runtime_tool_arguments_dict
from g3ku.providers.provider_factory import build_provider_from_model_key
from g3ku.providers.registry import find_by_name
from g3ku.runtime.config_refresh import refresh_loop_runtime_config
from g3ku.runtime.frontdoor.exposure_resolver import CeoExposureResolver
from g3ku.runtime.frontdoor.inline_tool_reminder import build_timeout_stop_error_text
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder
from g3ku.runtime.frontdoor.tool_contract import is_frontdoor_tool_contract_message
from g3ku.runtime.tool_watchdog import actor_role_allows_watchdog, run_tool_with_watchdog
from main.protocol import now_iso
from main.runtime.chat_backend import ConfigChatBackend, sanitize_provider_messages
from main.runtime.tool_call_repair import format_xml_repair_failure_reason


class _DirectProviderChatBackend:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model_refs: list[str],
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        parallel_tool_calls: bool | None = None,
        prompt_cache_key: str | None = None,
    ):
        model = str(model_refs[0] if model_refs else "").strip() or None
        kwargs: dict[str, Any] = {
            "messages": sanitize_provider_messages(messages),
            "tools": tools,
            "model": model,
            "tool_choice": "auto",
            "parallel_tool_calls": parallel_tool_calls,
            "prompt_cache_key": prompt_cache_key,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        return await self._provider.chat(**kwargs)


class CeoFrontDoorSupport:
    _CONTROL_TOOL_NAMES = {"stop_tool_execution"}

    def __init__(self, *, loop) -> None:
        self._loop = loop
        self._resolver = CeoExposureResolver(loop=loop)
        self._prompt_builder = CeoPromptBuilder(loop=loop)
        self._builder = CeoMessageBuilder(loop=loop, prompt_builder=self._prompt_builder)

    @staticmethod
    def _content_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        parts.append(text)
                    continue
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"image_url", "input_image"}:
                    parts.append("[image omitted]")
                    continue
                if item_type in {"file", "input_file"}:
                    filename = str(item.get("filename") or item.get("name") or "").strip()
                    parts.append(f"[file omitted: {filename}]" if filename else "[file omitted]")
                    continue
                text = item.get("text", item.get("content", ""))
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts).strip()
        return str(value or "")

    @classmethod
    def _is_empty_model_response(cls, response: Any) -> bool:
        if list(getattr(response, "tool_calls", None) or []):
            return False
        if cls._content_text(getattr(response, "content", "")).strip():
            return False
        if str(getattr(response, "error_text", None) or "").strip():
            return False
        if str(getattr(response, "reasoning_content", None) or "").strip():
            return False
        thinking_blocks = getattr(response, "thinking_blocks", None)
        if isinstance(thinking_blocks, list) and thinking_blocks:
            return False
        return True

    @staticmethod
    def _model_content(value: Any) -> Any:
        return value if isinstance(value, list) else str(value or "")

    @staticmethod
    def _empty_reply_fallback(query_text: str) -> str:
        snippet = " ".join(str(query_text or "").split()).strip()
        if len(snippet) > 64:
            snippet = f"{snippet[:61].rstrip()}..."
        if snippet:
            return f"No visible reply was generated for: {snippet}"
        return "No visible reply was generated."

    @staticmethod
    def _cron_internal_system_message(metadata: dict[str, Any]) -> dict[str, str] | None:
        if not bool(metadata.get("cron_internal")):
            return None
        job_id = str(metadata.get("cron_job_id") or "").strip()
        stop_condition = str(metadata.get("cron_stop_condition") or "user asked to stop").strip() or "user asked to stop"
        explicit = bool(metadata.get("cron_stop_condition_explicit"))
        lines = [
            "You are handling a cron-internal recurring job turn.",
            f"Current cron job id: {job_id or '(missing)'}",
            f"Exit condition: {stop_condition}",
            "Required behavior:",
            "- First inspect the current conversation context and the user's prior requests.",
            "- If the exit condition is already satisfied, or the user has clearly asked to stop/cancel this recurring task, immediately call the cron tool once with action='remove' and the current job_id.",
            "- After removing the current job, return one short plain-text confirmation only.",
            "- If the exit condition is not satisfied, do not call any tool and return plain text only.",
            "- Never call the message tool. Never create, update, list, or remove any other cron job.",
        ]
        if not explicit:
            lines.append("- This is a legacy cron job with no stored explicit exit condition; only 'user asked to stop' can end it.")
        return {"role": "system", "content": "\n".join(lines)}

    @staticmethod
    def _session_task_defaults(session_record: Any) -> dict[str, Any]:
        metadata = getattr(session_record, "metadata", None)
        if not isinstance(metadata, dict):
            return {}
        payload = metadata.get("task_defaults", metadata.get("taskDefaults"))
        if not isinstance(payload, dict):
            return {}
        max_depth = payload.get("max_depth", payload.get("maxDepth"))
        if max_depth in (None, ""):
            return dict(payload)
        return {
            **dict(payload),
            "max_depth": max_depth,
        }

    def _resolve_ceo_model_refs(self) -> list[str]:
        refresh_loop_runtime_config(self._loop, force=False, reason="ceo_frontdoor")
        app_config = getattr(self._loop, "app_config", None)
        if app_config is not None:
            refs = [
                str(ref or "").strip()
                for ref in app_config.get_role_model_keys("ceo")
                if str(ref or "").strip()
            ]
            if refs:
                cache_capable_refs = [
                    ref
                    for ref in refs
                    if self._model_ref_supports_prompt_cache(app_config, ref)
                ]
                if cache_capable_refs:
                    return cache_capable_refs
                return refs
        default_ref = f"{getattr(self._loop, 'provider_name', '')}:{getattr(self._loop, 'model', '')}".strip(":")
        return [default_ref] if default_ref else [str(getattr(self._loop, "model", "") or "").strip()]

    @staticmethod
    def _model_ref_supports_prompt_cache(app_config: Any, model_ref: str) -> bool:
        try:
            target = build_provider_from_model_key(app_config, str(model_ref or "").strip())
        except Exception:
            return False
        provider_id = str(getattr(target, "provider_id", "") or "").strip().lower()
        spec = find_by_name(provider_id)
        if spec is not None:
            return bool(spec.supports_prompt_caching)
        provider = getattr(target, "provider", None)
        supports_cache_control = getattr(provider, "_supports_cache_control", None)
        if callable(supports_cache_control):
            try:
                return bool(supports_cache_control(str(getattr(target, "model_id", "") or model_ref)))
            except Exception:
                return False
        return False

    def _resolve_chat_backend(self):
        app_config = getattr(self._loop, "app_config", None)
        if app_config is not None:
            return ConfigChatBackend(app_config)
        provider = getattr(self._loop, "provider", None)
        if provider is None:
            raise RuntimeError("CEO frontdoor requires an initialized provider or app_config.")
        return _DirectProviderChatBackend(provider)

    def _parallel_tool_settings(self) -> tuple[bool, int | None]:
        service = getattr(self._loop, "main_task_service", None)
        react_loop = getattr(service, "_react_loop", None) if service is not None else None
        enabled = bool(getattr(react_loop, "_parallel_tool_calls_enabled", True)) if react_loop is not None else True
        app_config = getattr(self._loop, "app_config", None)
        role_limit = (
            app_config.get_role_max_concurrency("ceo")
            if app_config is not None and hasattr(app_config, "get_role_max_concurrency")
            else None
        )
        max_parallel = role_limit if role_limit is not None else (
            getattr(react_loop, "_max_parallel_tool_calls", 10) if react_loop is not None else 10
        )
        return enabled, max_parallel

    def _registered_tools(self, tool_names: list[str]) -> dict[str, Tool]:
        tools: dict[str, Tool] = {}
        tool_registry = getattr(self._loop, "tools", None)
        getter = getattr(tool_registry, "get", None) if tool_registry is not None else None
        if not callable(getter):
            return tools
        for name in list(tool_names or []):
            tool = getter(str(name or "").strip())
            if isinstance(tool, Tool):
                tools[tool.name] = tool
        return tools

    @staticmethod
    def _route_kind_for_turn(*, used_tools: list[str], default: str) -> str:
        normalized = [str(name or "").strip() for name in list(used_tools or []) if str(name or "").strip()]
        if "create_async_task" in normalized:
            return "task_dispatch"
        if "continue_task" in normalized:
            return "task_continuation"
        if normalized:
            return "self_execute"
        return str(default or "direct_reply")

    @staticmethod
    def _empty_response_explanation(*, used_tools: list[str]) -> str:
        created_task = "create_async_task" in {
            str(name or "").strip()
            for name in list(used_tools or [])
            if str(name or "").strip()
        }
        continued_task = "continue_task" in {
            str(name or "").strip()
            for name in list(used_tools or [])
            if str(name or "").strip()
        }
        if created_task:
            return (
                "The turn completed without any visible assistant text after creating an async task. "
                "The system stopped instead of pretending a successful reply was produced."
            )
        if continued_task:
            return (
                "The turn completed without any visible assistant text after continuing an existing task. "
                "The system stopped instead of pretending a successful reply was produced."
            )
        return (
            "The turn completed without visible assistant text and without additional tool calls. "
            "The system stopped instead of pretending a successful reply was produced."
        )

    @staticmethod
    def _xml_repair_explanation(*, count: int, tool_names: list[str], content_excerpt: str) -> str:
        reason = format_xml_repair_failure_reason(
            count=count,
            tool_names=tool_names,
            content_excerpt=content_excerpt,
        )
        return (
            "XML pseudo tool-call repair failed repeatedly and the turn was stopped. "
            f"{reason}"
        )

    @staticmethod
    def _tool_invocation_hint(tool_name: str, arguments: dict[str, Any]) -> str:
        parts: list[str] = []
        for key, value in list(arguments.items())[:3]:
            preview = str(value or "").replace("\n", "\\n")
            if len(preview) > 48:
                preview = preview[:48].rstrip() + "..."
            parts.append(f"{key}={preview}")
        suffix = ", ".join(parts)
        return f"{tool_name} ({suffix})" if suffix else tool_name

    async def _emit_progress(
        self,
        on_progress,
        content: str,
        *,
        event_kind: str | None = None,
        event_data: dict[str, Any] | None = None,
        tool_hint: bool = False,
    ) -> None:
        if not on_progress:
            return
        try:
            result = on_progress(
                content,
                tool_hint=tool_hint,
                event_kind=event_kind,
                event_data=event_data or {},
            )
        except TypeError:
            result = on_progress(content, tool_hint=tool_hint)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _apply_turn_overlay(messages: list[dict[str, Any]], *, overlay_text: str | None) -> list[dict[str, Any]]:
        text = str(overlay_text or "").strip()
        if not text:
            return list(messages or [])
        base_messages = list(messages or [])
        overlay_block = f"System note for this turn only:\n{text}"
        return [*base_messages, {"role": "user", "content": overlay_block}]

    @staticmethod
    def _render_tool_result(result: Any) -> str:
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False)
        except Exception:
            return str(result)

    def _externalize_message_content(
        self,
        value: Any,
        *,
        runtime_context: dict[str, Any],
        display_name: str,
        source_kind: str,
        delivery_metadata: dict[str, Any] | None = None,
    ) -> Any:
        service = getattr(self._loop, "main_task_service", None)
        content_store = getattr(service, "content_store", None) if service is not None else None
        externalize = getattr(content_store, "externalize_for_message", None) if content_store is not None else None
        if not callable(externalize):
            return value
        return externalize(
            value,
            runtime=runtime_context,
            display_name=display_name,
            source_kind=source_kind,
            compact=True,
            delivery_metadata=delivery_metadata,
        )

    @staticmethod
    def _tool_result_delivery_metadata(*, tool: Tool | None) -> dict[str, Any]:
        descriptor = getattr(tool, "_descriptor", None)
        metadata = getattr(descriptor, "metadata", None) or {}
        return {
            "tool_result_inline_full": bool(
                getattr(descriptor, "tool_result_inline_full", False)
                or metadata.get("tool_result_inline_full", False)
            ),
        }

    @staticmethod
    def _tool_invocation_text(tool_name: str, arguments: dict[str, Any]) -> str:
        normalized_name = str(tool_name or "").strip() or "tool"
        normalized_arguments = dict(arguments or {})
        if not normalized_arguments:
            return normalized_name
        return f"{normalized_name}({json.dumps(normalized_arguments, ensure_ascii=False, sort_keys=True)})"

    def _render_tool_message_content(
        self,
        result: Any,
        *,
        runtime_context: dict[str, Any],
        tool_name: str,
        tool: Tool | None = None,
        delivery_metadata: dict[str, Any] | None = None,
    ) -> str:
        rendered = result if isinstance(result, str) else self._render_tool_result(result)
        return self._externalize_message_content(
            rendered,
            runtime_context=runtime_context,
            display_name=f"tool:{tool_name}",
            source_kind=f"tool_result:{tool_name}",
            delivery_metadata=delivery_metadata or self._tool_result_delivery_metadata(tool=tool),
        )

    @staticmethod
    def _tool_status(result_text: str) -> str:
        text = str(result_text or "").strip()
        return "error" if text.startswith("Error") else "success"

    @staticmethod
    def _tool_result_payload_ref(payload: dict[str, Any]) -> str:
        output_ref = str(
            payload.get("output_ref")
            or payload.get("resolved_ref")
            or payload.get("wrapper_ref")
            or payload.get("requested_ref")
            or payload.get("ref")
            or ""
        ).strip()
        if output_ref:
            return output_ref
        nested = parse_content_envelope(payload.get("content_ref"))
        if nested is not None:
            return str(nested.ref or nested.wrapper_ref or nested.resolved_ref or "").strip()
        return ""

    @staticmethod
    def _tool_result_progress_event_data(
        *,
        tool_name: str,
        result_text: str,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"tool_name": str(tool_name or "tool").strip() or "tool"}
        normalized_tool_call_id = str(tool_call_id or "").strip()
        if normalized_tool_call_id:
            payload["tool_call_id"] = normalized_tool_call_id
        text = str(result_text or "").strip()
        if not text:
            return payload
        envelope = parse_content_envelope(text)
        if envelope is not None:
            output_ref = str(envelope.resolved_ref or envelope.ref or envelope.wrapper_ref or "").strip()
            if output_ref:
                payload["output_ref"] = output_ref
            summary = str(envelope.summary or "").strip()
            if summary:
                payload["output_preview_text"] = summary
            return payload
        if text[:1] not in {"{", "["}:
            return payload
        try:
            parsed = json.loads(text)
        except Exception:
            return payload
        if not isinstance(parsed, dict):
            return payload
        output_ref = CeoFrontDoorSupport._tool_result_payload_ref(parsed)
        if output_ref:
            payload["output_ref"] = output_ref
        preview = str(parsed.get("summary") or "").strip()
        if preview:
            payload["output_preview_text"] = preview
        return payload

    async def _emit_watchdog_progress(self, *, on_progress, tool_name: str, poll: dict[str, Any]) -> None:
        snapshot = poll.get("snapshot") if isinstance(poll, dict) else None
        summary_text = str(snapshot.get("summary_text") or "").strip() if isinstance(snapshot, dict) else ""
        elapsed = float(poll.get("elapsed_seconds") or 0.0) if isinstance(poll, dict) else 0.0
        next_handoff = float(poll.get("next_handoff_in_seconds") or 0.0) if isinstance(poll, dict) else 0.0
        text = f"{tool_name} is still running after {elapsed:.0f}s."
        if summary_text:
            text = f"{text} Current snapshot: {summary_text}"
        if next_handoff > 0:
            text = f"{text} Another watchdog snapshot will be returned in about {next_handoff:.0f}s if needed."
        await self._emit_progress(
            on_progress,
            text,
            event_kind="tool",
            event_data={"tool_name": tool_name, "watchdog": True},
        )

    @staticmethod
    def _tool_result_message(
        *,
        tool_call_id: str,
        tool_name: str,
        content: Any,
        started_at: str,
        finished_at: str,
        elapsed_seconds: float | None,
    ) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": content,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": elapsed_seconds,
        }

    @staticmethod
    def _accepts_runtime_context(tool: Tool) -> bool:
        sig = inspect.signature(tool.execute)
        if "__g3ku_runtime" in sig.parameters:
            return True
        return any(param.kind is inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values())

    async def _execute_tool_call(
        self,
        *,
        tool: Tool,
        tool_name: str,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
        on_progress,
        tool_call_id: str | None = None,
    ) -> tuple[str, str, str, str, float | None]:
        _raw_result, rendered, status, started_at, finished_at, elapsed_seconds = await self._execute_tool_call_with_raw_result(
            tool=tool,
            tool_name=tool_name,
            arguments=arguments,
            runtime_context=runtime_context,
            on_progress=on_progress,
            tool_call_id=tool_call_id,
        )
        return rendered, status, started_at, finished_at, elapsed_seconds

    async def _execute_tool_call_with_raw_result(
        self,
        *,
        tool: Tool,
        tool_name: str,
        arguments: dict[str, Any],
        runtime_context: dict[str, Any],
        on_progress,
        tool_call_id: str | None = None,
    ) -> tuple[Any, str, str, str, str, float | None]:
        normalized_arguments = normalize_runtime_tool_arguments_dict(arguments)
        normalized_tool_call_id = str(tool_call_id or "").strip()
        inline_execution_id = ""
        try:
            errors = tool.validate_params(normalized_arguments)
        except Exception as exc:
            error_text = f"Error validating {tool_name}: {exc}"
            return error_text, error_text, "error", "", "", None
        if errors:
            error_text = f"Error: {'; '.join(errors)}"
            return error_text, error_text, "error", "", "", None

        started_at = now_iso()
        started_monotonic = time.monotonic()
        await self._emit_progress(
            on_progress,
            self._tool_invocation_hint(tool_name, normalized_arguments),
            event_kind="tool_start",
            event_data={
                "tool_name": tool_name,
                "arguments_text": self._tool_invocation_hint(tool_name, normalized_arguments),
                **({"tool_call_id": normalized_tool_call_id} if normalized_tool_call_id else {}),
            },
        )

        execute_kwargs = dict(normalized_arguments)
        per_call_runtime = {
            **runtime_context,
            "tool_name": tool_name,
            **({"tool_call_id": normalized_tool_call_id} if normalized_tool_call_id else {}),
        }
        if self._accepts_runtime_context(tool):
            execute_kwargs["__g3ku_runtime"] = per_call_runtime

        def _set_inline_execution_id(execution_id: str) -> None:
            nonlocal inline_execution_id
            inline_execution_id = str(execution_id or "").strip()

        async def _invoke() -> Any:
            resource_manager = getattr(self._loop, "resource_manager", None)
            if resource_manager is not None and resource_manager.get_tool_descriptor(tool_name) is not None:
                with resource_manager.acquire_tool(tool_name):
                    return await tool.execute(**execute_kwargs)
            return await tool.execute(**execute_kwargs)

        token = self._loop.tools.push_runtime_context(per_call_runtime)
        try:
            if actor_role_allows_watchdog(per_call_runtime) and str(tool_name or "").strip() != "continue_task":
                inline_registry = getattr(self._loop, "inline_tool_execution_registry", None)
                outcome = await run_tool_with_watchdog(
                    _invoke(),
                    tool_name=tool_name,
                    arguments=normalized_arguments,
                    runtime_context=per_call_runtime,
                    snapshot_supplier=runtime_context.get("tool_snapshot_supplier"),
                    manager=None,
                    inline_registry=inline_registry,
                    on_inline_registered=lambda entry: self._capture_inline_execution_id(
                        entry=entry,
                        target=lambda execution_id: _set_inline_execution_id(execution_id),
                    ),
                    on_poll=None,
                )
                result = outcome.value
            else:
                result = await _invoke()
        except asyncio.CancelledError:
            timeout_stop_error = self._inline_timeout_stop_error_text(
                tool_name=tool_name,
                execution_id=inline_execution_id,
            )
            if timeout_stop_error:
                finished_at = now_iso()
                elapsed_seconds = round(max(0.0, time.monotonic() - started_monotonic), 1)
                await self._emit_progress(
                    on_progress,
                    timeout_stop_error,
                    event_kind="tool_error",
                    event_data={
                        "tool_name": tool_name,
                        **({"tool_call_id": normalized_tool_call_id} if normalized_tool_call_id else {}),
                    },
                )
                return timeout_stop_error, timeout_stop_error, "error", started_at, finished_at, elapsed_seconds
            raise
        except Exception as exc:
            finished_at = now_iso()
            elapsed_seconds = round(max(0.0, time.monotonic() - started_monotonic), 1)
            error_text = self._inline_timeout_stop_error_text(
                tool_name=tool_name,
                execution_id=inline_execution_id,
            ) or f"Error executing {tool_name}: {exc}"
            await self._emit_progress(
                on_progress,
                error_text,
                event_kind="tool_error",
                event_data={
                    "tool_name": tool_name,
                    **({"tool_call_id": normalized_tool_call_id} if normalized_tool_call_id else {}),
                },
            )
            return error_text, error_text, "error", started_at, finished_at, elapsed_seconds
        finally:
            self._loop.tools.pop_runtime_context(token)
            inline_registry = getattr(self._loop, "inline_tool_execution_registry", None)
            if inline_execution_id and inline_registry is not None and hasattr(inline_registry, "discard_execution"):
                await inline_registry.discard_execution(inline_execution_id)

        rendered = self._render_tool_message_content(
            result,
            runtime_context=runtime_context,
            tool_name=tool_name,
            tool=tool,
            delivery_metadata={
                **self._tool_result_delivery_metadata(tool=tool),
                "invocation_text": self._tool_invocation_text(tool_name, normalized_arguments),
            },
        )
        finished_at = now_iso()
        elapsed_seconds = round(max(0.0, time.monotonic() - started_monotonic), 1)
        status = self._tool_status(rendered)
        return result, rendered, status, started_at, finished_at, elapsed_seconds

    @staticmethod
    async def _capture_inline_execution_id(*, entry: Any, target) -> None:
        execution_id = str(getattr(entry, "execution_id", "") or "").strip()
        if execution_id:
            target(execution_id)

    def _inline_timeout_stop_error_text(self, *, tool_name: str, execution_id: str) -> str:
        normalized_execution_id = str(execution_id or "").strip()
        if not normalized_execution_id:
            return ""
        inline_registry = getattr(self._loop, "inline_tool_execution_registry", None)
        if inline_registry is None or not hasattr(inline_registry, "stop_decision_metadata"):
            return ""
        metadata = inline_registry.stop_decision_metadata(normalized_execution_id)
        if not isinstance(metadata, dict) or str(metadata.get("reason_code") or "").strip() != "sidecar_timeout_stop":
            return ""
        return build_timeout_stop_error_text(
            tool_name=tool_name,
            stop_decision_metadata=metadata,
        )

    @staticmethod
    def _parallel_slot_count(limit: int | None, item_count: int, *, enabled: bool) -> int:
        if not enabled or item_count <= 1:
            return 1
        if limit is None:
            return max(1, item_count)
        return max(1, int(limit) if int(limit) > 0 else 1)
