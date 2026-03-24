from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from g3ku.agent.tools.base import Tool
from g3ku.runtime.config_refresh import refresh_loop_runtime_config
from g3ku.runtime.context import ContextAssemblyService
from g3ku.runtime.frontdoor.exposure_resolver import CeoExposureResolver
from g3ku.runtime.frontdoor.interaction_trace import (
    CEO_STAGE_STATUS_COMPLETED,
    CEO_STAGE_STATUS_FAILED,
    finalize_active_stage,
    is_transition_required,
    new_interaction_trace,
    normalize_interaction_trace,
    record_stage_round,
    stage_summary,
    submit_next_stage,
    update_round_tool,
)
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder
from g3ku.runtime.project_environment import current_project_environment
from g3ku.runtime.tool_watchdog import actor_role_allows_watchdog, run_tool_with_watchdog
from main.protocol import now_iso
from main.runtime.chat_backend import ConfigChatBackend, build_session_prompt_cache_key
from main.runtime.react_loop import RepeatedActionCircuitBreaker
from main.runtime.stage_budget import STAGE_TOOL_NAME, stage_gate_error_for_tool, visible_tools_for_stage_iteration
from main.runtime.stage_messages import build_ceo_stage_overlay


class _DirectProviderChatBackend:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model_refs: list[str],
        max_tokens: int = 1200,
        temperature: float = 0.2,
        reasoning_effort: str | None = None,
        parallel_tool_calls: bool | None = None,
        prompt_cache_key: str | None = None,
    ):
        model = str(model_refs[0] if model_refs else "").strip() or None
        return await self._provider.chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice="auto",
            parallel_tool_calls=parallel_tool_calls,
            prompt_cache_key=prompt_cache_key,
        )


class CeoSubmitNextStageTool(Tool):
    def __init__(self, submit_callback) -> None:
        self._submit_callback = submit_callback

    @property
    def name(self) -> str:
        return STAGE_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "Create or switch to the next CEO execution stage. "
            "Before using tools you must first create a stage; when the current stage budget is exhausted, "
            "you must create the next stage before using more tools."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "stage_goal": {
                    "type": "string",
                    "description": (
                        "A concise goal for the current CEO stage. Describe what this stage will accomplish "
                        "with the tools available to the CEO."
                    ),
                    "minLength": 1,
                },
                "tool_round_budget": {
                    "type": "integer",
                    "description": "How many ordinary tool rounds this stage may use. Must be between 1 and 10.",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["stage_goal", "tool_round_budget"],
        }

    async def execute(self, stage_goal: str, tool_round_budget: int, **kwargs: Any) -> str:
        result = await self._submit_callback(str(stage_goal or "").strip(), int(tool_round_budget or 0))
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


@dataclass(slots=True)
class CeoTurnResult:
    output: str
    route_kind: str
    interaction_trace: dict[str, Any] | None


class CeoFrontDoorRunner:
    _CONTROL_TOOL_NAMES = {"wait_tool_execution", "stop_tool_execution"}
    _CEO_NON_BUDGET_TOOLS = {"create_async_task"}

    def __init__(self, *, loop) -> None:
        self._loop = loop
        self._resolver = CeoExposureResolver(loop=loop)
        self._prompt_builder = CeoPromptBuilder(loop=loop)
        self._assembly = ContextAssemblyService(loop=loop, prompt_builder=self._prompt_builder)

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

    @staticmethod
    def _model_content(value: Any) -> Any:
        return value if isinstance(value, list) else str(value or "")

    @staticmethod
    def _empty_reply_fallback(query_text: str) -> str:
        snippet = " ".join(str(query_text or "").split()).strip()
        if len(snippet) > 32:
            snippet = f"{snippet[:29].rstrip()}..."
        if snippet:
            return f"这次没有生成可展示的回复。请直接再发一次“{snippet}”，我会继续处理。"
        return "这次没有生成可展示的回复。请直接再发一次你的请求，我会继续处理。"

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

    def _parallel_tool_settings(self) -> tuple[bool, int]:
        service = getattr(self._loop, "main_task_service", None)
        react_loop = getattr(service, "_react_loop", None) if service is not None else None
        enabled = bool(getattr(react_loop, "_parallel_tool_calls_enabled", True)) if react_loop is not None else True
        max_parallel = max(1, int(getattr(react_loop, "_max_parallel_tool_calls", 10) or 10)) if react_loop is not None else 10
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
            "arguments_text": json.dumps(arguments, ensure_ascii=False, indent=2) if isinstance(arguments, dict) else str(arguments or ""),
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

    def _stage_gate(self, trace: dict[str, Any] | None) -> dict[str, Any]:
        normalized = normalize_interaction_trace(trace)
        active = next(
            (
                stage
                for stage in reversed(list(normalized.get("stages") or []))
                if str(stage.get("status") or "").strip() == "active"
            ),
            None,
        )
        completed = [
            dict(stage)
            for stage in list(normalized.get("stages") or [])
            if str(stage.get("status") or "").strip() != "active"
        ]
        return {
            "has_active_stage": active is not None,
            "transition_required": is_transition_required(normalized),
            "active_stage": dict(active or {}) if isinstance(active, dict) else None,
            "completed_stages": completed,
        }

    def _sync_session_trace(self, session: Any, trace: dict[str, Any] | None) -> None:
        normalized = normalize_interaction_trace(trace)
        summary = stage_summary(normalized, transition_required=is_transition_required(normalized))
        if hasattr(session, "set_interaction_trace"):
            session.set_interaction_trace(normalized if normalized.get("stages") else None, stage=summary)
            return
        setattr(session, "_interaction_trace", normalized if normalized.get("stages") else None)
        setattr(session, "_current_stage", summary)

    @staticmethod
    def _apply_stage_overlay(messages: list[dict[str, Any]], *, overlay_text: str | None) -> list[dict[str, Any]]:
        text = str(overlay_text or "").strip()
        if not text:
            return list(messages or [])
        return [{"role": "system", "content": text}, *list(messages or [])]

    @staticmethod
    def _externalize_message_content(value: Any, *, runtime_context: dict[str, Any]) -> Any:
        service = getattr(runtime_context.get("loop"), "main_task_service", None)
        store = getattr(getattr(service, "log_service", None), "_content_store", None)
        if store is None:
            return value
        return store.externalize_for_message(
            value,
            runtime=runtime_context,
            display_name=f"ceo:{runtime_context.get('session_key') or 'session'}",
            source_kind="ceo_message",
            compact=True,
        )

    def _externalize_tool_result(self, value: Any, *, runtime_context: dict[str, Any], tool_name: str) -> Any:
        service = getattr(runtime_context.get("loop"), "main_task_service", None)
        store = getattr(getattr(service, "log_service", None), "_content_store", None)
        if store is None:
            return value
        return store.externalize_for_message(
            value,
            runtime=runtime_context,
            display_name=f"tool:{tool_name}",
            source_kind=f"tool_result:{tool_name}",
            compact=True,
        )

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
        if text.startswith("Error"):
            return "error"
        return "success"

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
        if bool(runtime_context.get("stage_turn_granted")) and tool_name != STAGE_TOOL_NAME:
            stage_gate = {"has_active_stage": True, "transition_required": False}
        else:
            stage_gate = self._stage_gate(runtime_context.get("interaction_trace"))
        stage_gate_error = stage_gate_error_for_tool(
            tool_name,
            has_active_stage=bool(stage_gate.get("has_active_stage")),
            transition_required=bool(stage_gate.get("transition_required")),
            stage_tool_name=STAGE_TOOL_NAME,
        )
        if stage_gate_error:
            return f"Error: {stage_gate_error}", "error", "", "", None

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

        externalized = self._externalize_tool_result(result, runtime_context=runtime_context, tool_name=tool_name)
        rendered = self._render_tool_result(externalized)
        finished_at = now_iso()
        elapsed_seconds = round(max(0.0, time.monotonic() - started_monotonic), 1)
        status = self._tool_status(rendered)
        await self._emit_progress(
            on_progress,
            rendered,
            event_kind="tool_result" if status == "success" else "tool_error",
            event_data={"tool_name": tool_name},
        )
        return rendered, status, started_at, finished_at, elapsed_seconds

    async def _run_react_turn(
        self,
        *,
        session: Any,
        messages: list[dict[str, Any]],
        tools: dict[str, Tool],
        model_refs: list[str],
        runtime_context: dict[str, Any],
        prompt_cache_key: str,
    ) -> CeoTurnResult:
        limit = max(2, int(getattr(self._loop, "max_iterations", 12) or 12))
        parallel_enabled, max_parallel_tool_calls = self._parallel_tool_settings()
        message_history = list(messages or [])
        interaction_trace = new_interaction_trace()
        route_kind = "direct_reply"
        used_tools: list[str] = []
        breaker = RepeatedActionCircuitBreaker()

        async def _submit_stage(stage_goal: str, tool_round_budget: int) -> dict[str, Any]:
            nonlocal interaction_trace
            interaction_trace, stage = submit_next_stage(
                interaction_trace,
                stage_goal=stage_goal,
                tool_round_budget=tool_round_budget,
            )
            self._sync_session_trace(session, interaction_trace)
            return dict(stage)

        all_tools = {
            **dict(tools or {}),
            STAGE_TOOL_NAME: CeoSubmitNextStageTool(_submit_stage),
        }
        chat_backend = self._resolve_chat_backend()

        for _attempt in range(limit):
            stage_gate = self._stage_gate(interaction_trace)
            visible_tools = visible_tools_for_stage_iteration(
                all_tools,
                has_active_stage=bool(stage_gate.get("has_active_stage")),
                transition_required=bool(stage_gate.get("transition_required")),
                stage_tool_name=STAGE_TOOL_NAME,
            )
            tool_schemas = [tool.to_schema() for tool in visible_tools.values()]
            request_messages = self._apply_stage_overlay(
                message_history,
                overlay_text=build_ceo_stage_overlay(self._stage_gate(interaction_trace)),
            )
            response = await chat_backend.chat(
                messages=request_messages,
                tools=tool_schemas or None,
                model_refs=model_refs,
                max_tokens=1200,
                temperature=0.2,
                parallel_tool_calls=(parallel_enabled if tool_schemas else None),
                prompt_cache_key=prompt_cache_key,
            )
            response_tool_calls = list(response.tool_calls or [])
            tool_call_payloads = [self._tool_call_payload(call) for call in response_tool_calls]

            if response_tool_calls:
                analysis_text = self._content_text(getattr(response, "content", ""))
                if analysis_text.strip():
                    await self._emit_progress(
                        runtime_context.get("on_progress"),
                        analysis_text.strip(),
                        event_kind="analysis",
                    )
                if any(payload["name"] not in self._CONTROL_TOOL_NAMES for payload in tool_call_payloads):
                    for payload in tool_call_payloads:
                        signature = f"{payload['name']}:{json.dumps(payload['arguments'], ensure_ascii=False, sort_keys=True)}"
                        if payload["name"] != STAGE_TOOL_NAME and payload["name"] not in self._CONTROL_TOOL_NAMES:
                            breaker.register(signature)
                if any(payload["name"] == STAGE_TOOL_NAME for payload in tool_call_payloads) and len(tool_call_payloads) != 1:
                    assistant_message = {
                        "role": "assistant",
                        "content": self._externalize_message_content(response.content, runtime_context=runtime_context),
                        "tool_calls": self._assistant_tool_calls(response_tool_calls),
                    }
                    message_history.append(assistant_message)
                    for payload in tool_call_payloads:
                        message_history.append(
                            self._tool_result_message(
                                tool_call_id=str(payload.get("id") or ""),
                                tool_name=str(payload.get("name") or "tool"),
                                content="Error: submit_next_stage must be the only tool call in its turn",
                                started_at="",
                                finished_at="",
                                elapsed_seconds=None,
                            )
                        )
                    continue

                round_payload = None
                active_stage_id = str((stage_gate.get("active_stage") or {}).get("stage_id") or "")
                if bool(stage_gate.get("has_active_stage")) and not bool(stage_gate.get("transition_required")):
                    interaction_trace, round_payload = record_stage_round(
                        interaction_trace,
                        tool_calls=tool_call_payloads,
                        extra_non_budget_tools=self._CEO_NON_BUDGET_TOOLS,
                    )
                    self._sync_session_trace(session, interaction_trace)

                semaphore = asyncio.Semaphore(max_parallel_tool_calls if parallel_enabled else 1)

                async def _run_single(index: int):
                    nonlocal interaction_trace
                    payload = tool_call_payloads[index]
                    tool_name = str(payload.get("name") or "")
                    tool = all_tools.get(tool_name)
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
                                runtime_context={
                                    **runtime_context,
                                    "interaction_trace": interaction_trace,
                                    "stage_turn_granted": bool(
                                        stage_gate.get("has_active_stage")
                                        and not stage_gate.get("transition_required")
                                    ),
                                },
                                on_progress=runtime_context.get("on_progress"),
                            )
                    if isinstance(round_payload, dict):
                        interaction_trace = update_round_tool(
                            interaction_trace,
                            stage_id=active_stage_id,
                            round_id=str(round_payload.get("round_id") or ""),
                            tool_call_id=str(payload.get("id") or ""),
                            output_text=result_text,
                            status=status,
                            finished_at=finished_at,
                            elapsed_seconds=elapsed_seconds,
                        )
                        self._sync_session_trace(session, interaction_trace)
                    return self._tool_result_message(
                        tool_call_id=str(payload.get("id") or ""),
                        tool_name=tool_name or "tool",
                        content=result_text,
                        started_at=started_at,
                        finished_at=finished_at,
                        elapsed_seconds=elapsed_seconds,
                    )

                tool_messages = await asyncio.gather(
                    *[_run_single(index) for index in range(len(response_tool_calls))]
                )
                assistant_message = {
                    "role": "assistant",
                    "content": self._externalize_message_content(response.content, runtime_context=runtime_context),
                    "tool_calls": self._assistant_tool_calls(response_tool_calls),
                }
                message_history.append(assistant_message)
                message_history.extend(tool_messages)
                used_tools.extend(
                    [
                        str(payload.get("name") or "").strip()
                        for payload in tool_call_payloads
                        if str(payload.get("name") or "").strip()
                    ]
                )
                if "create_async_task" in used_tools:
                    route_kind = "task_dispatch"
                elif used_tools:
                    route_kind = "self_execute"
                continue

            text = self._content_text(getattr(response, "content", ""))
            if text.strip():
                interaction_trace["final_output"] = text.strip()
                interaction_trace = finalize_active_stage(
                    interaction_trace,
                    status=CEO_STAGE_STATUS_COMPLETED,
                )
                self._sync_session_trace(session, interaction_trace)
                if "create_async_task" in used_tools:
                    route_kind = "task_dispatch"
                elif used_tools:
                    route_kind = "self_execute"
                return CeoTurnResult(
                    output=text.strip(),
                    route_kind=route_kind,
                    interaction_trace=interaction_trace if interaction_trace.get("stages") else None,
                )

            if str(getattr(response, "finish_reason", "") or "").strip().lower() == "error":
                raise RuntimeError(str(getattr(response, "error_text", None) or response.content or "model response failed"))
            return CeoTurnResult(
                output="",
                route_kind="task_dispatch" if "create_async_task" in used_tools else ("self_execute" if used_tools else route_kind),
                interaction_trace=interaction_trace if interaction_trace.get("stages") else None,
            )

        interaction_trace = finalize_active_stage(interaction_trace, status=CEO_STAGE_STATUS_FAILED)
        self._sync_session_trace(session, interaction_trace)
        raise RuntimeError("CEO frontdoor exceeded maximum iterations")

    async def run_turn(self, *, user_input, session, on_progress=None) -> str:
        await self._loop._ensure_checkpointer_ready()
        query_text = self._content_text(getattr(user_input, "content", ""))
        metadata = dict(getattr(user_input, "metadata", None) or {})
        heartbeat_internal = bool(metadata.get("heartbeat_internal"))
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
        assembly = await self._assembly.build_for_ceo(
            session=session,
            query_text=query_text,
            exposure=exposure,
            persisted_session=persisted_session,
        )
        tool_names = list(assembly.tool_names or list(exposure.get("tool_names") or []))
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": assembly.system_prompt},
            *list(assembly.recent_history or []),
            {"role": "user", "content": self._model_content(getattr(user_input, "content", ""))},
        ]
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
        }

        setattr(session, "_last_route_kind", "direct_reply")
        token = self._loop.tools.push_runtime_context(runtime_context)
        try:
            result = await self._run_react_turn(
                session=session,
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
