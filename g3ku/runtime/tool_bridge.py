from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger
from g3ku.content import parse_content_envelope
from g3ku.runtime.tool_watchdog import (
    actor_role_allows_watchdog,
    resolve_snapshot_supplier,
    run_tool_with_watchdog,
    runtime_context_value,
)

try:
    from langchain_core.messages import ToolMessage
except ModuleNotFoundError:  # pragma: no cover - optional dependency fallback
    class ToolMessage:  # type: ignore[no-redef]
        def __init__(self, content: str = "", tool_call_id: str = "", name: str = "", status: str = "success"):
            self.content = content
            self.tool_call_id = tool_call_id
            self.name = name
            self.status = status

if TYPE_CHECKING:
    from g3ku.agent.loop import AgentLoop

_CONTROL_TOOL_NAMES = {"wait_tool_execution", "stop_tool_execution"}


class ToolExecutionBridge:
    """Shared tool execution and formatting bridge for runtime integrations."""

    def __init__(self, loop: "AgentLoop"):
        self._loop = loop

    @staticmethod
    def _event_data(runtime_context: Any, **extra: Any) -> dict[str, Any]:
        def _keep(value: Any) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, (list, dict, tuple, set)):
                return len(value) > 0
            return True

        payload: dict[str, Any] = {}
        trace_meta = getattr(runtime_context, 'trace_meta', None) if runtime_context is not None else None
        if isinstance(trace_meta, dict):
            payload.update({key: value for key, value in trace_meta.items() if _keep(value)})
        payload.update({key: value for key, value in extra.items() if _keep(value)})
        return payload

    def set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        for name in ("message", "cron"):
            if tool := self._loop.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _should_use_watchdog(*, runtime_context: Any, tool_name: str) -> bool:
        if tool_name in _CONTROL_TOOL_NAMES:
            return False
        return actor_role_allows_watchdog(runtime_context)

    @staticmethod
    def preview(value: Any, *, max_chars: int = 400) -> str:
        text = str(value or "").replace("\n", "\\n").strip()
        if len(text) <= max_chars:
            return text
        return f"{text[:max_chars]}...(truncated {len(text) - max_chars} chars)"

    @staticmethod
    def summarize_tool_call(tool_call: Any) -> tuple[str, dict[str, Any]]:
        if isinstance(tool_call, dict):
            name = str(tool_call.get("name") or "")
            args = tool_call.get("args", tool_call.get("arguments", {}))
            if isinstance(args, str):
                return name, {"raw": ToolExecutionBridge.preview(args)}
            if not isinstance(args, dict):
                return name, {}
            return name, args

        name = str(getattr(tool_call, "name", ""))
        args = getattr(tool_call, "arguments", {})
        if isinstance(args, list):
            args = args[0] if args else {}
        if isinstance(args, str):
            return name, {"raw": ToolExecutionBridge.preview(args)}
        if not isinstance(args, dict):
            return name, {}
        return name, args

    @staticmethod
    def tool_invocation_hint(tool_call: Any) -> str:
        name, args = ToolExecutionBridge.summarize_tool_call(tool_call)
        if not args:
            return name or "tool"
        parts = []
        for key, value in list(args.items())[:3]:
            parts.append(f"{key}={ToolExecutionBridge.preview(value, max_chars=48)}")
        suffix = ", ".join(parts)
        return f"{name} ({suffix})" if name else suffix

    @staticmethod
    def tool_result_hint(tool_name: str, content: Any) -> str:
        envelope = parse_content_envelope(content)
        if envelope is not None:
            return f"{tool_name} finished | {envelope.summary}"
        if isinstance(content, list):
            counts = {"text": 0, "image": 0, "file": 0, "other": 0}
            text_bits: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    item_type = str(item.get("type") or "").strip().lower()
                    if item_type in {"text", "input_text", "output_text"}:
                        counts["text"] += 1
                        snippet = item.get("text", item.get("content", ""))
                        if isinstance(snippet, str) and snippet:
                            text_bits.append(snippet)
                    elif item_type in {"image_url", "input_image"}:
                        counts["image"] += 1
                    elif item_type in {"file", "input_file"}:
                        counts["file"] += 1
                    else:
                        counts["other"] += 1
                elif isinstance(item, str) and item:
                    counts["text"] += 1
                    text_bits.append(item)
                else:
                    counts["other"] += 1
            fragments = []
            if counts["text"]:
                fragments.append(f"{counts['text']} text")
            if counts["image"]:
                fragments.append(f"{counts['image']} image")
            if counts["file"]:
                fragments.append(f"{counts['file']} file")
            if counts["other"]:
                fragments.append(f"{counts['other']} other")
            preview = ToolExecutionBridge.preview(" ".join(text_bits), max_chars=160)
            summary = ", ".join(fragments) if fragments else "multimodal output"
            return f"{tool_name} finished: {summary}" + (f" | {preview}" if preview else "")

        if isinstance(content, str):
            stripped = content.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    parts = []
                    if "status" in parsed:
                        parts.append(f"status={parsed.get('status')}")
                    if "success" in parsed:
                        parts.append(f"success={parsed.get('success')}")
                    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
                    stage = data.get("stage") if isinstance(data, dict) else None
                    if stage:
                        parts.append(f"stage={stage}")
                    detail = (
                        parsed.get("message")
                        or parsed.get("error")
                        or parsed.get("reason")
                        or parsed.get("blocked_reason")
                        or parsed.get("result")
                    )
                    hint = data.get("hint") if isinstance(data, dict) else None
                    detail_parts = []
                    if detail is not None:
                        detail_parts.append(str(detail))
                    if hint and hint not in detail_parts:
                        detail_parts.append(f"Hint: {hint}")
                    detail_text = ToolExecutionBridge.preview(" | ".join(detail_parts), max_chars=220) if detail_parts else ""
                    return f"{tool_name} finished" + (f": {', '.join(parts)}" if parts else "") + (f" | {detail_text}" if detail_text else "")
            return f"{tool_name} finished | {ToolExecutionBridge.preview(stripped, max_chars=180)}"

        return f"{tool_name} finished | {ToolExecutionBridge.preview(content, max_chars=180)}"

    @staticmethod
    def tool_hint(tool_calls: list[Any]) -> str:
        def _extract_name_and_args(tc: Any) -> tuple[str, dict[str, Any]]:
            if isinstance(tc, dict):
                name = str(tc.get("name") or "")
                args = tc.get("args", {})
                return name, args if isinstance(args, dict) else {}

            name = str(getattr(tc, "name", ""))
            args = getattr(tc, "arguments", {})
            if isinstance(args, list):
                args = args[0] if args else {}
            return name, args if isinstance(args, dict) else {}

        def _fmt(tc: Any) -> str:
            name, args = _extract_name_and_args(tc)
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return name or "tool"
            short = ToolExecutionBridge.preview(val, max_chars=36)
            return f'{name}("{short}")' if name else short

        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def apply_before_middlewares(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        iteration: int,
        session_key: str | None,
    ) -> dict[str, Any]:
        req = {"name": name, "arguments": arguments}
        for mw in self._loop.middlewares:
            hook = getattr(mw, "before_tool", None)
            if not hook:
                continue
            try:
                update = await self._loop._maybe_await(
                    hook(
                        name=req["name"],
                        arguments=req["arguments"],
                        iteration=iteration,
                        session_key=session_key,
                    )
                )
            except Exception:
                logger.exception("Middleware before_tool failed: {}", type(mw).__name__)
                continue

            if update is None:
                continue
            if not isinstance(update, dict):
                logger.warning(
                    "Middleware before_tool must return dict|None, got {} from {}",
                    type(update).__name__,
                    type(mw).__name__,
                )
                continue

            if update.get("name"):
                req["name"] = update["name"]
            if "arguments" in update and update["arguments"] is not None:
                req["arguments"] = update["arguments"]

        return req

    async def apply_after_middlewares(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        result: Any,
        iteration: int,
        session_key: str | None,
    ) -> Any:
        current = result
        for mw in self._loop.middlewares:
            hook = getattr(mw, "after_tool", None)
            if not hook:
                continue
            try:
                maybe_new = await self._loop._maybe_await(
                    hook(
                        name=name,
                        arguments=arguments,
                        result=current,
                        iteration=iteration,
                        session_key=session_key,
                    )
                )
            except Exception:
                logger.exception("Middleware after_tool failed: {}", type(mw).__name__)
                continue
            if maybe_new is not None:
                current = maybe_new
        return current

    async def wrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        runtime_context = getattr(request.runtime, "context", None)
        channel = getattr(runtime_context, "channel", None)
        chat_id = getattr(runtime_context, "chat_id", None)
        message_id = getattr(runtime_context, "message_id", None)
        on_progress = getattr(runtime_context, "on_progress", None)
        if channel and chat_id:
            self.set_tool_context(channel, chat_id, message_id)

        tool_name = None
        tool_call = getattr(request, "tool_call", None)
        if isinstance(tool_call, dict):
            tool_name = tool_call.get("name")
        elif tool_call is not None:
            tool_name = getattr(tool_call, "name", None)
        if self._loop.debug_trace:
            name, args = self.summarize_tool_call(tool_call)
            logger.info(
                "[debug:tool:call] session={} channel={} chat={} tool={} args={}",
                getattr(runtime_context, "session_key", None),
                channel,
                chat_id,
                name or tool_name or "-",
                self._loop._preview(args, max_chars=1200),
            )

        if on_progress and (tool_name or tool_call is not None):
            invocation = self.tool_invocation_hint(tool_call)
            await self._loop._emit_progress_event(
                on_progress,
                invocation,
                event_kind="tool_start",
                event_data=self._event_data(runtime_context, tool_name=tool_name or "tool"),
            )

        inherited_runtime = dict(self._loop.tools.get_runtime_context() or {}) if hasattr(self._loop.tools, "get_runtime_context") else {}
        token = self._loop.tools.push_runtime_context(
            {
                **inherited_runtime,
                "on_progress": on_progress,
                "actor_role": getattr(runtime_context, "actor_role", None),
                "session_key": getattr(runtime_context, "session_key", None),
                "channel": channel,
                "chat_id": chat_id,
                "message_id": message_id,
                "tool_name": tool_name,
                "skip_tool_registry_watchdog": True,
                "cancel_token": getattr(runtime_context, "cancel_token", None),
                "tool_snapshot_supplier": runtime_context_value(runtime_context, "tool_snapshot_supplier", inherited_runtime.get("tool_snapshot_supplier")),
                "tool_watchdog": runtime_context_value(runtime_context, "tool_watchdog", inherited_runtime.get("tool_watchdog")),
                "temp_dir": str(self._loop.temp_dir),
                "loop": self._loop,
            }
        )
        try:
            if self._should_use_watchdog(runtime_context=runtime_context, tool_name=str(tool_name or "tool")):
                outcome = await run_tool_with_watchdog(
                    handler(request),
                    tool_name=str(tool_name or "tool"),
                    arguments=self.summarize_tool_call(tool_call)[1],
                    runtime_context=runtime_context if runtime_context is not None else self._loop.tools.get_runtime_context(),
                    snapshot_supplier=self._resolve_watchdog_snapshot_supplier(runtime_context),
                    manager=getattr(self._loop, "tool_execution_manager", None),
                    on_poll=(
                        (lambda poll: self._emit_watchdog_progress(on_progress=on_progress, runtime_context=runtime_context, poll=poll))
                        if on_progress
                        else None
                    ),
                )
                if outcome.completed:
                    result = outcome.value
                else:
                    raw_call_id = ""
                    if isinstance(tool_call, dict):
                        raw_call_id = str(tool_call.get("id") or "")
                    elif tool_call is not None:
                        raw_call_id = str(getattr(tool_call, "id", "") or "")
                    result = ToolMessage(
                        content=self._stringify_tool_result(outcome.value),
                        tool_call_id=raw_call_id or f"{tool_name or 'tool'}:watchdog",
                        name=str(tool_name or "tool"),
                        status="success",
                    )
            else:
                result = await handler(request)
            result_content = self._externalize_tool_result(
                getattr(result, "content", ""),
                runtime_context=runtime_context,
                tool_name=str(tool_name or "tool"),
            )
            if result_content != getattr(result, "content", ""):
                result = self._replace_tool_message_content(result, result_content)
            if self._loop.debug_trace:
                logger.info(
                    "[debug:tool:result] session={} channel={} chat={} tool={} content={}",
                    getattr(runtime_context, "session_key", None),
                    channel,
                    chat_id,
                    tool_name or "-",
                    self._loop._preview(result_content, max_chars=1200),
                )
            if on_progress:
                await self._loop._emit_progress_event(
                    on_progress,
                    self.tool_result_hint(tool_name or "tool", result_content),
                    event_kind="tool_result",
                    event_data=self._event_data(runtime_context, tool_name=tool_name or "tool"),
                )
            return result
        except Exception as exc:
            if self._loop.debug_trace:
                logger.exception(
                    "[debug:tool:error] session={} channel={} chat={} tool={} error={}",
                    getattr(runtime_context, "session_key", None),
                    channel,
                    chat_id,
                    tool_name or "-",
                    exc,
                )
            if on_progress:
                await self._loop._emit_progress_event(
                    on_progress,
                    f"{tool_name or 'tool'} failed: {exc}",
                    event_kind="tool_error",
                    event_data=self._event_data(runtime_context, tool_name=tool_name or "tool"),
                )
            raise
        finally:
            self._loop.tools.pop_runtime_context(token)

    async def execute_named_tool(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        tool_call_id: str,
        runtime_context: Any = None,
        emit_progress: bool = False,
    ) -> ToolMessage:
        channel = getattr(runtime_context, "channel", None) if runtime_context else None
        chat_id = getattr(runtime_context, "chat_id", None) if runtime_context else None
        message_id = getattr(runtime_context, "message_id", None) if runtime_context else None
        on_progress = getattr(runtime_context, "on_progress", None) if runtime_context else None
        session_key = getattr(runtime_context, "session_key", None) if runtime_context else None
        iteration = int(getattr(runtime_context, "iteration", 1)) if runtime_context else 1
        if channel and chat_id:
            self.set_tool_context(channel, chat_id, message_id)

        tool_req = await self.apply_before_middlewares(
            name=name,
            arguments=arguments,
            iteration=iteration,
            session_key=session_key,
        )
        tool_name = str(tool_req["name"])
        tool_args = tool_req["arguments"] if isinstance(tool_req["arguments"], dict) else {}

        if on_progress and emit_progress:
            await self._loop._emit_progress_event(
                on_progress,
                self.tool_invocation_hint({"name": tool_name, "args": tool_args}),
                event_kind="tool_start",
                event_data=self._event_data(runtime_context, tool_name=tool_name or "tool"),
            )

        logger.info("Tool call: {}({})", tool_name, json.dumps(tool_args, ensure_ascii=False)[:200])
        inherited_runtime = dict(self._loop.tools.get_runtime_context() or {}) if hasattr(self._loop.tools, "get_runtime_context") else {}
        token = self._loop.tools.push_runtime_context(
            {
                **inherited_runtime,
                "on_progress": on_progress,
                "actor_role": getattr(runtime_context, "actor_role", None) if runtime_context else None,
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "message_id": message_id,
                "tool_name": tool_name,
                "skip_tool_registry_watchdog": True,
                "cancel_token": getattr(runtime_context, "cancel_token", None) if runtime_context else None,
                "tool_snapshot_supplier": runtime_context_value(runtime_context, "tool_snapshot_supplier", inherited_runtime.get("tool_snapshot_supplier")),
                "tool_watchdog": runtime_context_value(runtime_context, "tool_watchdog", inherited_runtime.get("tool_watchdog")),
                "temp_dir": str(self._loop.temp_dir),
                "loop": self._loop,
            }
        )
        try:
            resource_manager = getattr(self._loop, 'resource_manager', None)
            async def _execute_tool_call() -> Any:
                if resource_manager is not None and resource_manager.get_tool_descriptor(tool_name) is not None:
                    with resource_manager.acquire_tool(tool_name):
                        return await self._loop.tools.execute(tool_name, tool_args)
                return await self._loop.tools.execute(tool_name, tool_args)

            if self._should_use_watchdog(runtime_context=runtime_context, tool_name=tool_name):
                outcome = await run_tool_with_watchdog(
                    _execute_tool_call(),
                    tool_name=tool_name,
                    arguments=tool_args,
                    runtime_context=runtime_context if runtime_context is not None else self._loop.tools.get_runtime_context(),
                    snapshot_supplier=self._resolve_watchdog_snapshot_supplier(runtime_context),
                    manager=getattr(self._loop, "tool_execution_manager", None),
                    on_poll=(
                        (lambda poll: self._emit_watchdog_progress(on_progress=on_progress, runtime_context=runtime_context, poll=poll))
                        if on_progress and emit_progress
                        else None
                    ),
                )
                result = outcome.value
            else:
                result = await _execute_tool_call()
            result = await self.apply_after_middlewares(
                name=tool_name,
                arguments=tool_args,
                result=result,
                iteration=iteration,
                session_key=session_key,
            )
        finally:
            self._loop.tools.pop_runtime_context(token)

        result = self._externalize_tool_result(
            result,
            runtime_context=runtime_context,
            tool_name=tool_name,
        )
        rendered = self._stringify_tool_result(result)
        if on_progress and emit_progress:
            event_kind = "tool_error" if rendered.startswith("Error") else "tool_result"
            progress_text = f"{tool_name} failed: {rendered}" if event_kind == "tool_error" else self.tool_result_hint(tool_name, rendered)
            await self._loop._emit_progress_event(
                on_progress,
                progress_text,
                event_kind=event_kind,
                event_data=self._event_data(runtime_context, tool_name=tool_name or "tool"),
            )
        status = "error" if rendered.startswith("Error") else "success"
        return ToolMessage(
            content=rendered,
            tool_call_id=tool_call_id,
            name=tool_name,
            status=status,
        )

    @staticmethod
    def _stringify_tool_result(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)

    def _resolve_watchdog_snapshot_supplier(self, runtime_context: Any):
        supplier = resolve_snapshot_supplier(runtime_context)
        if supplier is not None:
            return supplier
        registry_runtime = self._loop.tools.get_runtime_context() if hasattr(self._loop.tools, "get_runtime_context") else {}
        supplier = resolve_snapshot_supplier(registry_runtime)
        if supplier is not None:
            return supplier
        task_id = runtime_context_value(runtime_context, "task_id", None)
        main_service = getattr(self._loop, "main_task_service", None)
        if task_id and main_service is not None and hasattr(main_service, "get_task_detail_payload"):
            return lambda: main_service.get_task_detail_payload(str(task_id), mark_read=False)
        return None

    async def _emit_watchdog_progress(self, *, on_progress, runtime_context: Any, poll: dict[str, Any]) -> None:
        snapshot = poll.get("snapshot") if isinstance(poll, dict) else None
        summary_text = str(snapshot.get("summary_text") or "").strip() if isinstance(snapshot, dict) else ""
        elapsed = float(poll.get("elapsed_seconds") or 0.0) if isinstance(poll, dict) else 0.0
        next_handoff = float(poll.get("next_handoff_in_seconds") or 0.0) if isinstance(poll, dict) else 0.0
        tool_name = str(poll.get("tool_name") or runtime_context_value(runtime_context, "tool_name", None) or "tool")
        text = f"{tool_name} 仍在处理中，已等待 {elapsed:.0f} 秒。"
        if summary_text:
            text = f"{text} 当前看到的阶段：{summary_text}"
        else:
            text = f"{text} 暂时还没有新的阶段快照。"
        if next_handoff > 0:
            text = f"{text} 如果还没完成，我会在约 {next_handoff:.0f} 秒后把新的运行快照交回给 agent。"
        await self._loop._emit_progress_event(
            on_progress,
            text,
            event_kind="tool",
            event_data=self._event_data(runtime_context, tool_name=tool_name, watchdog=True),
        )

    def _externalize_tool_result(self, value: Any, *, runtime_context: Any, tool_name: str) -> Any:
        service = getattr(self._loop, 'main_task_service', None)
        store = getattr(service, 'content_store', None) if service is not None else None
        if store is None:
            return value
        runtime_payload = {
            'session_key': getattr(runtime_context, 'session_key', None) if runtime_context is not None else None,
            'task_id': getattr(runtime_context, 'task_id', None) if runtime_context is not None else None,
            'node_id': getattr(runtime_context, 'node_id', None) if runtime_context is not None else None,
        }
        result = store.externalize_for_message(
            value,
            runtime=runtime_payload,
            display_name=f'tool:{tool_name}',
            source_kind=f'tool_result:{tool_name}',
            compact=True,
        )
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return result

    @staticmethod
    def _replace_tool_message_content(message: Any, content: Any) -> Any:
        if hasattr(message, 'model_copy'):
            return message.model_copy(update={'content': content})
        try:
            return ToolMessage(
                content=content,
                tool_call_id=getattr(message, 'tool_call_id', ''),
                name=getattr(message, 'name', ''),
                status=getattr(message, 'status', 'success'),
            )
        except Exception:
            return message


