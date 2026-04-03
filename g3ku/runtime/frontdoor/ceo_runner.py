from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from g3ku.agent.tools.base import Tool
from g3ku.providers.fallback import PUBLIC_PROVIDER_FAILURE_MESSAGE
from g3ku.runtime.config_refresh import refresh_loop_runtime_config
from g3ku.runtime.frontdoor.exposure_resolver import CeoExposureResolver
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder
from g3ku.runtime.project_environment import current_project_environment
from g3ku.runtime.tool_watchdog import actor_role_allows_watchdog, run_tool_with_watchdog
from main.protocol import now_iso
from main.runtime.chat_backend import ConfigChatBackend, build_session_prompt_cache_key
from main.runtime.react_loop import RepeatedActionCircuitBreaker
from main.runtime.tool_call_repair import (
    XML_REPAIR_ATTEMPT_LIMIT,
    build_xml_tool_repair_message,
    extract_tool_calls_from_xml_pseudo_content,
    format_xml_repair_failure_reason,
    recover_tool_calls_from_json_payload,
)


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
            "messages": messages,
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


@dataclass(slots=True)
class CeoTurnResult:
    output: str
    route_kind: str


class CeoFrontDoorRunner:
    _CONTROL_TOOL_NAMES = {"wait_tool_execution", "stop_tool_execution"}
    _EMPTY_RESPONSE_RETRY_LIMIT = 1

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
                return refs
        default_ref = f"{getattr(self._loop, 'provider_name', '')}:{getattr(self._loop, 'model', '')}".strip(":")
        return [default_ref] if default_ref else [str(getattr(self._loop, "model", "") or "").strip()]

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
        for name in list(tool_names or []):
            tool = self._loop.tools.get(str(name or "").strip())
            if isinstance(tool, Tool):
                tools[tool.name] = tool
        return tools

    @staticmethod
    def _tool_call_payload(call: Any) -> dict[str, Any]:
        arguments = dict(getattr(call, "arguments", {}) or {})
        return {
            "id": str(getattr(call, "id", "") or ""),
            "name": str(getattr(call, "name", "") or "").strip(),
            "arguments": arguments,
        }

    @staticmethod
    def _assistant_tool_calls(response_tool_calls: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": str(getattr(call, "id", "") or ""),
                "type": "function",
                "function": {
                    "name": str(getattr(call, "name", "") or "").strip(),
                    "arguments": json.dumps(dict(getattr(call, "arguments", {}) or {}), ensure_ascii=False),
                },
            }
            for call in list(response_tool_calls or [])
        ]

    @staticmethod
    def _route_kind_for_turn(*, used_tools: list[str], default: str) -> str:
        normalized = [str(name or "").strip() for name in list(used_tools or []) if str(name or "").strip()]
        if "create_async_task" in normalized:
            return "task_dispatch"
        if normalized:
            return "self_execute"
        return str(default or "direct_reply")

    @staticmethod
    def _empty_response_retry_message(*, visible_tool_names: list[str]) -> str:
        visible = ", ".join(f"`{name}`" for name in list(visible_tool_names or [])[:8]) or "(none)"
        return (
            "System note: your previous model turn was empty: no visible text and no tool calls. "
            "Do not return an empty reply. "
            "Either call a visible tool or provide the final visible answer. "
            f"Visible tools this turn: {visible}."
        )

    @staticmethod
    def _empty_response_explanation(*, used_tools: list[str]) -> str:
        created_task = "create_async_task" in {
            str(name or "").strip()
            for name in list(used_tools or [])
            if str(name or "").strip()
        }
        if created_task:
            return (
                "The turn completed without any visible assistant text after creating an async task. "
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
        if base_messages and str(base_messages[-1].get("role") or "").strip().lower() == "user":
            last_message = dict(base_messages[-1])
            last_content = last_message.get("content")
            if isinstance(last_content, str):
                last_message["content"] = (
                    f"{last_content.rstrip()}\n\n{overlay_block}"
                    if last_content.strip()
                    else overlay_block
                )
                return [*base_messages[:-1], last_message]
        return [*base_messages, {"role": "user", "content": overlay_block}]

    @staticmethod
    def _render_tool_result(result: Any) -> str:
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False)
        except Exception:
            return str(result)

    @staticmethod
    def _tool_status(result_text: str) -> str:
        text = str(result_text or "").strip()
        return "error" if text.startswith("Error") else "success"

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
    ) -> tuple[str, str, str, str, float | None]:
        errors = tool.validate_params(arguments)
        if errors:
            return f"Error: {'; '.join(errors)}", "error", "", "", None

        started_at = now_iso()
        started_monotonic = time.monotonic()
        await self._emit_progress(
            on_progress,
            self._tool_invocation_hint(tool_name, arguments),
            event_kind="tool_start",
            event_data={"tool_name": tool_name},
        )

        execute_kwargs = dict(arguments)
        per_call_runtime = {
            **runtime_context,
            "tool_name": tool_name,
        }
        if self._accepts_runtime_context(tool):
            execute_kwargs["__g3ku_runtime"] = per_call_runtime

        async def _invoke() -> Any:
            resource_manager = getattr(self._loop, "resource_manager", None)
            if resource_manager is not None and resource_manager.get_tool_descriptor(tool_name) is not None:
                with resource_manager.acquire_tool(tool_name):
                    return await tool.execute(**execute_kwargs)
            return await tool.execute(**execute_kwargs)

        token = self._loop.tools.push_runtime_context(per_call_runtime)
        try:
            if actor_role_allows_watchdog(per_call_runtime):
                outcome = await run_tool_with_watchdog(
                    _invoke(),
                    tool_name=tool_name,
                    arguments=arguments,
                    runtime_context=per_call_runtime,
                    snapshot_supplier=runtime_context.get("tool_snapshot_supplier"),
                    manager=getattr(self._loop, "tool_execution_manager", None),
                    on_poll=(
                        (lambda poll: self._emit_watchdog_progress(on_progress=on_progress, tool_name=tool_name, poll=poll))
                        if on_progress
                        else None
                    ),
                )
                result = outcome.value
            else:
                result = await _invoke()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            finished_at = now_iso()
            elapsed_seconds = round(max(0.0, time.monotonic() - started_monotonic), 1)
            error_text = f"Error executing {tool_name}: {exc}"
            await self._emit_progress(
                on_progress,
                error_text,
                event_kind="tool_error",
                event_data={"tool_name": tool_name},
            )
            return error_text, "error", started_at, finished_at, elapsed_seconds
        finally:
            self._loop.tools.pop_runtime_context(token)

        rendered = self._render_tool_result(result)
        finished_at = now_iso()
        elapsed_seconds = round(max(0.0, time.monotonic() - started_monotonic), 1)
        status = self._tool_status(rendered)
        return rendered, status, started_at, finished_at, elapsed_seconds

    async def _run_react_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: dict[str, Tool],
        model_refs: list[str],
        runtime_context: dict[str, Any],
        prompt_cache_key: str,
    ) -> CeoTurnResult:
        configured_limit = getattr(self._loop, "max_iterations", 12)
        parallel_enabled, max_parallel_tool_calls = self._parallel_tool_settings()
        message_history = list(messages or [])
        route_kind = "direct_reply"
        used_tools: list[str] = []
        repair_overlay_text: str | None = None
        xml_repair_attempt_count = 0
        xml_repair_excerpt = ""
        xml_repair_tool_names: list[str] = []
        xml_repair_last_issue = ""
        breaker = RepeatedActionCircuitBreaker()
        chat_backend = self._resolve_chat_backend()

        attempt_index = 0
        while configured_limit is None or attempt_index < max(0, int(configured_limit)):
            attempt_index += 1
            request_messages = self._apply_turn_overlay(message_history, overlay_text=repair_overlay_text)
            repair_overlay_text = None
            tool_schemas = [tool.to_schema() for tool in tools.values()]
            provider_retry_count = 0
            empty_response_retry_count = 0
            while True:
                try:
                    response = await chat_backend.chat(
                        messages=request_messages,
                        tools=tool_schemas or None,
                        model_refs=model_refs,
                        parallel_tool_calls=(parallel_enabled if tool_schemas else None),
                        prompt_cache_key=prompt_cache_key,
                    )
                except Exception as exc:
                    if PUBLIC_PROVIDER_FAILURE_MESSAGE not in str(exc or ""):
                        raise
                    provider_retry_count += 1
                    await asyncio.sleep(float(min(10, max(1, provider_retry_count))))
                    continue
                if self._is_empty_model_response(response):
                    empty_response_retry_count += 1
                    await asyncio.sleep(float(min(10, max(1, empty_response_retry_count))))
                    continue
                break
            visible_tool_names = {
                str(name or "").strip()
                for name in tools.keys()
                if str(name or "").strip()
            }
            response_tool_calls = list(response.tool_calls or [])
            synthetic_tool_calls_used = False
            xml_pseudo_call = None
            if not response_tool_calls and visible_tool_names:
                xml_extraction = extract_tool_calls_from_xml_pseudo_content(
                    response.content,
                    visible_tools=tools,
                    id_prefix="call:ceo-xml-direct",
                )
                if xml_extraction.tool_calls:
                    response_tool_calls = xml_extraction.tool_calls
                    synthetic_tool_calls_used = True
                if not response_tool_calls and xml_repair_attempt_count > 0:
                    repaired_tool_calls = recover_tool_calls_from_json_payload(
                        response.content,
                        allowed_tool_names=visible_tool_names,
                        id_prefix="call:ceo-xml-repair",
                    )
                    if repaired_tool_calls:
                        response_tool_calls = repaired_tool_calls
                        synthetic_tool_calls_used = True
                if not response_tool_calls and xml_extraction.matched:
                    xml_pseudo_call = {
                        "excerpt": xml_extraction.excerpt,
                        "tool_names": list(xml_extraction.tool_names or []),
                        "issue": str(xml_extraction.issue or "").strip(),
                    }
            tool_call_payloads = [self._tool_call_payload(call) for call in response_tool_calls]

            if response_tool_calls:
                if xml_repair_attempt_count > 0:
                    xml_repair_attempt_count = 0
                    xml_repair_excerpt = ""
                    xml_repair_tool_names = []
                    xml_repair_last_issue = ""
                analysis_text = "" if synthetic_tool_calls_used else self._content_text(getattr(response, "content", ""))
                if analysis_text.strip():
                    await self._emit_progress(
                        runtime_context.get("on_progress"),
                        analysis_text.strip(),
                        event_kind="analysis",
                    )
                for payload in tool_call_payloads:
                    signature = f"{payload['name']}:{json.dumps(payload['arguments'], ensure_ascii=False, sort_keys=True)}"
                    if payload["name"] not in self._CONTROL_TOOL_NAMES:
                        breaker.register(signature)

                semaphore = asyncio.Semaphore(
                    self._parallel_slot_count(max_parallel_tool_calls, len(tool_call_payloads), enabled=parallel_enabled)
                )

                async def _run_single(index: int):
                    payload = tool_call_payloads[index]
                    tool_name = str(payload.get("name") or "")
                    tool = tools.get(tool_name)
                    if tool is None:
                        result_text = f"Error: tool not available: {tool_name}"
                        status = "error"
                        started_at = ""
                        finished_at = ""
                        elapsed_seconds = None
                    else:
                        async with semaphore:
                            result_text, status, started_at, finished_at, elapsed_seconds = await self._execute_tool_call(
                                tool=tool,
                                tool_name=tool_name,
                                arguments=dict(payload.get("arguments") or {}),
                                runtime_context=runtime_context,
                                on_progress=runtime_context.get("on_progress"),
                            )
                    await self._emit_progress(
                        runtime_context.get("on_progress"),
                        result_text,
                        event_kind="tool_result" if status == "success" else "tool_error",
                        event_data={"tool_name": tool_name},
                    )
                    return self._tool_result_message(
                        tool_call_id=str(payload.get("id") or ""),
                        tool_name=tool_name or "tool",
                        content=result_text,
                        started_at=started_at,
                        finished_at=finished_at,
                        elapsed_seconds=elapsed_seconds,
                    )

                tool_messages = await asyncio.gather(*[_run_single(index) for index in range(len(response_tool_calls))])
                assistant_message = {
                    "role": "assistant",
                    "content": None if synthetic_tool_calls_used else self._model_content(getattr(response, "content", "")),
                    "tool_calls": self._assistant_tool_calls(response_tool_calls),
                }
                message_history.append(assistant_message)
                message_history.extend(tool_messages)
                used_tools.extend(
                    [
                        str(payload.get("name") or "").strip()
                        for payload in tool_call_payloads
                        if str(payload.get("name") or "").strip() and str(payload.get("name") or "").strip() not in self._CONTROL_TOOL_NAMES
                    ]
                )
                route_kind = self._route_kind_for_turn(used_tools=used_tools, default=route_kind)
                continue

            if xml_pseudo_call is not None:
                xml_repair_attempt_count += 1
                xml_repair_excerpt = str(xml_pseudo_call.get("excerpt") or "").strip()
                xml_repair_tool_names = list(xml_pseudo_call.get("tool_names") or [])
                xml_repair_last_issue = (
                    str(xml_pseudo_call.get("issue") or "").strip()
                    or "reply used XML-like pseudo tool syntax instead of a valid tool call"
                )
                if xml_repair_attempt_count >= XML_REPAIR_ATTEMPT_LIMIT:
                    return CeoTurnResult(
                        output=self._xml_repair_explanation(
                            count=xml_repair_attempt_count,
                            tool_names=xml_repair_tool_names,
                            content_excerpt=xml_repair_excerpt,
                        ),
                        route_kind=self._route_kind_for_turn(used_tools=used_tools, default=route_kind),
                    )
                repair_overlay_text = build_xml_tool_repair_message(
                    xml_excerpt=xml_repair_excerpt,
                    tool_names=xml_repair_tool_names,
                    attempt_count=xml_repair_attempt_count,
                    attempt_limit=XML_REPAIR_ATTEMPT_LIMIT,
                    latest_issue=xml_repair_last_issue,
                )
                continue

            if xml_repair_attempt_count > 0:
                xml_repair_attempt_count += 1
                xml_repair_last_issue = "reply still did not contain valid structured tool_calls or a valid JSON repair payload"
                if xml_repair_attempt_count >= XML_REPAIR_ATTEMPT_LIMIT:
                    return CeoTurnResult(
                        output=self._xml_repair_explanation(
                            count=xml_repair_attempt_count,
                            tool_names=xml_repair_tool_names,
                            content_excerpt=str(response.content or ""),
                        ),
                        route_kind=self._route_kind_for_turn(used_tools=used_tools, default=route_kind),
                    )
                repair_overlay_text = build_xml_tool_repair_message(
                    xml_excerpt=xml_repair_excerpt,
                    tool_names=xml_repair_tool_names,
                    attempt_count=xml_repair_attempt_count,
                    attempt_limit=XML_REPAIR_ATTEMPT_LIMIT,
                    latest_issue=xml_repair_last_issue,
                )
                continue

            text = self._content_text(getattr(response, "content", ""))
            if text.strip():
                return CeoTurnResult(
                    output=text.strip(),
                    route_kind=self._route_kind_for_turn(used_tools=used_tools, default=route_kind),
                )

            if str(getattr(response, "finish_reason", "") or "").strip().lower() == "error":
                raise RuntimeError(str(getattr(response, "error_text", None) or response.content or "model response failed"))
            return CeoTurnResult(
                output=self._empty_response_explanation(used_tools=used_tools),
                route_kind=self._route_kind_for_turn(used_tools=used_tools, default=route_kind),
            )

        raise RuntimeError("CEO frontdoor exceeded maximum iterations")

    @staticmethod
    def _parallel_slot_count(limit: int | None, item_count: int, *, enabled: bool) -> int:
        if not enabled or item_count <= 1:
            return 1
        if limit is None:
            return max(1, item_count)
        return max(1, int(limit) if int(limit) > 0 else 1)

    async def run_turn(self, *, user_input, session, on_progress=None) -> str:
        await self._loop._ensure_checkpointer_ready()
        query_text = self._content_text(getattr(user_input, "content", ""))
        metadata = dict(getattr(user_input, "metadata", None) or {})
        heartbeat_internal = bool(metadata.get("heartbeat_internal"))
        cron_internal = bool(metadata.get("cron_internal"))
        runtime_session = self._loop.sessions.get_or_create(session.state.session_key)
        persisted_session = runtime_session
        main_service = getattr(self._loop, "main_task_service", None)
        if main_service is not None:
            await main_service.startup()
        memory_channel = getattr(session, "_memory_channel", getattr(session, "_channel", "cli"))
        memory_chat_id = getattr(session, "_memory_chat_id", getattr(session, "_chat_id", session.state.session_key))
        for name in ("message", "cron"):
            tool = self._loop.tools.get(name)
            if tool is not None and hasattr(tool, "set_context"):
                if name == "message":
                    tool.set_context(getattr(session, "_channel", "cli"), getattr(session, "_chat_id", session.state.session_key), None)
                else:
                    tool.set_context(getattr(session, "_channel", "cli"), getattr(session, "_chat_id", session.state.session_key))
        message_tool = self._loop.tools.get("message")
        if message_tool is not None and hasattr(message_tool, "start_turn"):
            message_tool.start_turn()

        exposure = await self._resolver.resolve_for_actor(actor_role="ceo", session_id=session.state.session_key)
        assembly = await self._builder.build_for_ceo(
            session=session,
            query_text=query_text,
            exposure=exposure,
            persisted_session=persisted_session,
            user_content=self._model_content(getattr(user_input, "content", "")),
            user_metadata=metadata,
        )
        tool_names = list(assembly.tool_names or list(exposure.get("tool_names") or []))
        if cron_internal:
            tool_names = ["cron"]
        cron_system_message = self._cron_internal_system_message(metadata)
        messages: list[dict[str, Any]] = list(assembly.model_messages or [])
        if cron_system_message is not None:
            insert_at = 1 if messages and str(messages[0].get("role") or "").strip().lower() == "system" else 0
            messages = [*messages[:insert_at], cron_system_message, *messages[insert_at:]]
        if not messages or str(messages[-1].get("role") or "").strip().lower() != "user":
            messages.append({"role": "user", "content": self._model_content(getattr(user_input, "content", ""))})

        project_environment = current_project_environment(workspace_root=getattr(self._loop, "workspace", None))
        session_task_defaults = self._session_task_defaults(runtime_session)
        model_refs = self._resolve_ceo_model_refs()
        provider_model = str(model_refs[0] if model_refs else "").strip()
        stable_prompt_cache_key = build_session_prompt_cache_key(
            session_key=str(getattr(session.state, "session_key", "") or ""),
            provider_model=provider_model,
            scope="ceo_frontdoor",
        )
        runtime_context = {
            "on_progress": on_progress,
            "emit_lifecycle": True,
            "actor_role": "ceo",
            "session_key": session.state.session_key,
            "channel": getattr(session, "_channel", "cli"),
            "chat_id": getattr(session, "_chat_id", session.state.session_key),
            "memory_channel": memory_channel,
            "memory_chat_id": memory_chat_id,
            "cancel_token": getattr(session, "_active_cancel_token", None),
            "tool_snapshot_supplier": getattr(session, "inflight_turn_snapshot", None),
            "temp_dir": str(getattr(self._loop, "temp_dir", "") or ""),
            "loop": self._loop,
            "task_defaults": session_task_defaults,
            "project_python": str(project_environment.get("project_python") or ""),
            "project_python_dir": str(project_environment.get("project_python_dir") or ""),
            "project_scripts_dir": str(project_environment.get("project_scripts_dir") or ""),
            "project_path_entries": list(project_environment.get("project_path_entries") or []),
            "project_virtual_env": str(project_environment.get("project_virtual_env") or ""),
            "project_python_hint": str(project_environment.get("project_python_hint") or ""),
            "heartbeat_internal": heartbeat_internal,
            "cron_internal": cron_internal,
            "cron_job_id": str(metadata.get("cron_job_id") or "").strip(),
            "cron_stop_condition": str(metadata.get("cron_stop_condition") or "").strip(),
        }

        setattr(session, "_last_route_kind", "direct_reply")
        token = self._loop.tools.push_runtime_context(runtime_context)
        try:
            result = await self._run_react_turn(
                messages=messages,
                tools=self._registered_tools(tool_names),
                model_refs=model_refs,
                runtime_context=runtime_context,
                prompt_cache_key=stable_prompt_cache_key,
            )
        finally:
            self._loop.tools.pop_runtime_context(token)

        output = str(result.output or "").strip()
        if not output and not heartbeat_internal:
            logger.warning(
                "ceo frontdoor produced empty visible output; session_key={} route_kind={}",
                str(getattr(session.state, "session_key", "") or ""),
                result.route_kind,
            )
            output = self._empty_reply_fallback(query_text)
        setattr(session, "_last_route_kind", str(result.route_kind or "direct_reply"))
        return output
