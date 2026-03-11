"""Runtime middleware utilities and official AgentMiddleware factories."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from inspect import isclass
from typing import Any

from loguru import logger

try:
    from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse, ToolCallRequest
    from langchain_core.messages import SystemMessage, ToolMessage
except ModuleNotFoundError:  # pragma: no cover - optional dependency fallback
    AgentMiddleware = object  # type: ignore[assignment]
    ModelRequest = Any  # type: ignore[assignment]
    ModelResponse = Any  # type: ignore[assignment]
    ToolCallRequest = Any  # type: ignore[assignment]

    class SystemMessage:  # type: ignore[no-redef]
        def __init__(self, content: str = ""):
            self.content = content
            self.text = content

    class ToolMessage:  # type: ignore[no-redef]
        def __init__(self, content: str = "", name: str = ""):
            self.content = content
            self.name = name

        def model_copy(self, update: dict[str, Any] | None = None):
            update = update or {}
            return ToolMessage(
                content=str(update.get("content", self.content)),
                name=str(update.get("name", self.name)),
            )

from g3ku.config.schema import AgentMiddlewareConfig


@dataclass(slots=True)
class PrependSystemMessageMiddleware(AgentMiddleware):
    """Prepend a static system message before each model call."""

    text: str
    role: str = "system"

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler,
    ) -> ModelResponse[Any]:
        if self.role.strip().lower() != "system":
            # LangChain create_agent currently supports a single system message.
            # Non-system roles are normalized to system for compatibility.
            normalized_text = f"[{self.role}] {self.text}"
        else:
            normalized_text = self.text

        if request.system_message is not None and request.system_message.text:
            merged = f"{normalized_text}\n\n{request.system_message.text}"
        else:
            merged = normalized_text

        patched = request.override(system_message=SystemMessage(content=merged))
        return await handler(patched)


@dataclass(slots=True)
class ToolResultDecoratorMiddleware(AgentMiddleware):
    """Decorate tool results with prefix/suffix."""

    prefix: str = ""
    suffix: str = ""

    async def awrap_tool_call(self, request: ToolCallRequest, handler) -> ToolMessage:
        message = await handler(request)
        content = message.content
        if isinstance(content, str):
            return message.model_copy(update={"content": f"{self.prefix}{content}{self.suffix}"})
        return message


def _load_object_from_path(class_path: str) -> Any:
    """Load a Python object from 'module:attr' or 'module.attr' path."""
    path = class_path.strip()
    if not path:
        raise ValueError("empty class path")

    if ":" in path:
        module_name, attr_name = path.split(":", 1)
    else:
        module_name, _, attr_name = path.rpartition(".")

    module_name = module_name.strip()
    attr_name = attr_name.strip()
    if not module_name or not attr_name:
        raise ValueError(
            "classPath must be in format 'package.module:Name' or 'package.module.Name'"
        )

    module = import_module(module_name)
    return getattr(module, attr_name)


def _instantiate_custom_middleware(cfg: AgentMiddlewareConfig) -> AgentMiddleware | None:
    """Instantiate custom middleware from class_path and options."""
    class_path = (cfg.class_path or "").strip()
    if not class_path:
        return None

    options = cfg.options or {}
    obj = _load_object_from_path(class_path)

    if isclass(obj):
        instance = obj(**options)
    elif callable(obj):
        instance = obj(**options)
    else:
        raise ValueError(
            "Invalid middleware classPath target.\n"
            f"Original field: classPath={class_path!r}\n"
            "New requirement: classPath must resolve to an AgentMiddleware class/factory.\n"
            "Example fix: classPath='my_pkg.middleware:MyAgentMiddleware'"
        )

    if not isinstance(instance, AgentMiddleware):
        legacy_hooks = [
            hook
            for hook in ("before_llm", "after_llm", "before_tool", "after_tool")
            if callable(getattr(instance, hook, None))
        ]
        if legacy_hooks:
            hooks = ", ".join(legacy_hooks)
            raise ValueError(
                "Legacy middleware hooks are no longer supported.\n"
                f"Original middleware hooks: {hooks}\n"
                "New requirement: implement official AgentMiddleware lifecycle methods.\n"
                "Example fix: use awrap_model_call / awrap_tool_call on an AgentMiddleware subclass."
            )
        raise ValueError(
            "Custom middleware must be an AgentMiddleware instance.\n"
            f"Original classPath: {class_path!r}\n"
            "New requirement: return an AgentMiddleware object.\n"
            "Example fix: class MyMiddleware(AgentMiddleware): ..."
        )

    return instance


def build_middlewares(configs: list[AgentMiddlewareConfig] | None) -> list[AgentMiddleware]:
    """Build AgentMiddleware instances from config entries (strict mode)."""
    if not configs:
        return []

    built: list[AgentMiddleware] = []
    for cfg in configs:
        if not cfg.enabled:
            continue

        custom = _instantiate_custom_middleware(cfg)
        if custom is not None:
            built.append(custom)
            continue

        name = (cfg.name or "").strip().lower()
        options = cfg.options or {}

        if name in {"before_llm", "after_llm", "before_tool", "after_tool"}:
            raise ValueError(
                "Legacy middleware config is no longer supported.\n"
                f"Original field: agents.defaults.middlewares[].name = {cfg.name!r}\n"
                "New requirement: use built-in official middlewares or AgentMiddleware classPath.\n"
                "Example fix: {'enabled': true, 'name': 'prepend_system_message', 'options': {'text': '...'}}"
            )

        if name == "prepend_system_message":
            text = options.get("text")
            role = options.get("role", "system")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(
                    "Invalid prepend_system_message middleware config.\n"
                    f"Original options: {options}\n"
                    "New required field: options.text (non-empty string).\n"
                    "Example fix: {'enabled': true, 'name': 'prepend_system_message', 'options': {'text': 'Be concise.'}}"
                )
            if not isinstance(role, str) or not role.strip():
                role = "system"
            built.append(PrependSystemMessageMiddleware(text=text, role=role))
            continue

        if name in {"tool_result_suffix", "tool_result_decorator"}:
            prefix = options.get("prefix", "")
            suffix = options.get("suffix", "")
            if not isinstance(prefix, str):
                prefix = ""
            if not isinstance(suffix, str):
                suffix = ""
            built.append(ToolResultDecoratorMiddleware(prefix=prefix, suffix=suffix))
            continue

        if name in {"tool_call_limit", "toolcall_limit"}:
            try:
                from langchain.agents.middleware.tool_call_limit import ToolCallLimitMiddleware
            except Exception as exc:
                raise ValueError(
                    "tool_call_limit middleware requires LangChain built-in middleware support.\n"
                    f"Original options: {options}\n"
                    "New requirement: install langchain>=1.2 runtime dependencies."
                ) from exc

            tool_name = options.get("tool_name", options.get("toolName"))
            thread_limit = options.get("thread_limit", options.get("threadLimit"))
            run_limit = options.get("run_limit", options.get("runLimit"))
            exit_behavior = options.get("exit_behavior", options.get("exitBehavior", "continue"))
            built.append(
                ToolCallLimitMiddleware(
                    tool_name=tool_name if isinstance(tool_name, str) and tool_name.strip() else None,
                    thread_limit=thread_limit if isinstance(thread_limit, int) else None,
                    run_limit=run_limit if isinstance(run_limit, int) else None,
                    exit_behavior=exit_behavior if exit_behavior in {"continue", "error"} else "continue",
                )
            )
            continue

        if name == "tool_retry":
            try:
                from langchain.agents.middleware.tool_retry import ToolRetryMiddleware
            except Exception as exc:
                raise ValueError(
                    "tool_retry middleware requires LangChain built-in middleware support.\n"
                    f"Original options: {options}\n"
                    "New requirement: install langchain>=1.2 runtime dependencies."
                ) from exc

            max_retries = options.get("max_retries", options.get("maxRetries", 2))
            tools = options.get("tools")
            on_failure = options.get("on_failure", options.get("onFailure", "continue"))
            backoff_factor = options.get("backoff_factor", options.get("backoffFactor", 2.0))
            initial_delay = options.get("initial_delay", options.get("initialDelay", 1.0))
            max_delay = options.get("max_delay", options.get("maxDelay", 60.0))
            jitter = options.get("jitter", True)

            kwargs: dict[str, Any] = {}
            if isinstance(max_retries, int):
                kwargs["max_retries"] = max_retries
            if isinstance(tools, list):
                kwargs["tools"] = tools
            if on_failure in {"continue", "error"}:
                kwargs["on_failure"] = on_failure
            if isinstance(backoff_factor, (int, float)):
                kwargs["backoff_factor"] = float(backoff_factor)
            if isinstance(initial_delay, (int, float)):
                kwargs["initial_delay"] = float(initial_delay)
            if isinstance(max_delay, (int, float)):
                kwargs["max_delay"] = float(max_delay)
            if isinstance(jitter, bool):
                kwargs["jitter"] = jitter
            built.append(ToolRetryMiddleware(**kwargs))
            continue

        if name == "model_retry":
            try:
                from langchain.agents.middleware.model_retry import ModelRetryMiddleware
            except Exception as exc:
                raise ValueError(
                    "model_retry middleware requires LangChain built-in middleware support.\n"
                    f"Original options: {options}\n"
                    "New requirement: install langchain>=1.2 runtime dependencies."
                ) from exc

            max_retries = options.get("max_retries", options.get("maxRetries", 2))
            on_failure = options.get("on_failure", options.get("onFailure", "continue"))
            backoff_factor = options.get("backoff_factor", options.get("backoffFactor", 2.0))
            initial_delay = options.get("initial_delay", options.get("initialDelay", 1.0))
            max_delay = options.get("max_delay", options.get("maxDelay", 60.0))
            jitter = options.get("jitter", True)

            kwargs = {}
            if isinstance(max_retries, int):
                kwargs["max_retries"] = max_retries
            if on_failure in {"continue", "error"}:
                kwargs["on_failure"] = on_failure
            if isinstance(backoff_factor, (int, float)):
                kwargs["backoff_factor"] = float(backoff_factor)
            if isinstance(initial_delay, (int, float)):
                kwargs["initial_delay"] = float(initial_delay)
            if isinstance(max_delay, (int, float)):
                kwargs["max_delay"] = float(max_delay)
            if isinstance(jitter, bool):
                kwargs["jitter"] = jitter
            built.append(ModelRetryMiddleware(**kwargs))
            continue

        if name in {"llm_tool_selector", "tool_selection"}:
            try:
                from langchain.agents.middleware.tool_selection import LLMToolSelectorMiddleware
            except Exception as exc:
                raise ValueError(
                    "llm_tool_selector middleware requires LangChain built-in middleware support.\n"
                    f"Original options: {options}\n"
                    "New requirement: install langchain>=1.2 runtime dependencies."
                ) from exc

            model = options.get("model")
            system_prompt = options.get("system_prompt", options.get("systemPrompt"))
            max_tools = options.get("max_tools", options.get("maxTools"))
            always_include = options.get("always_include", options.get("alwaysInclude"))
            min_tool_count = options.get("min_tool_count", options.get("minToolCount", 8))
            min_query_chars = options.get("min_query_chars", options.get("minQueryChars", 24))
            require_complex_query = options.get(
                "require_complex_query", options.get("requireComplexQuery", True)
            )
            complex_terms = options.get("complex_terms", options.get("complexTerms"))
            avoid_query_terms = options.get("avoid_query_terms", options.get("avoidQueryTerms"))
            skip_provider_types = options.get("skip_provider_types", options.get("skipProviderTypes"))
            force_provider_types = options.get("force_provider_types", options.get("forceProviderTypes"))
            kwargs = {}
            if isinstance(model, str) and model.strip():
                kwargs["model"] = model
            if isinstance(system_prompt, str) and system_prompt.strip():
                kwargs["system_prompt"] = system_prompt
            if isinstance(max_tools, int):
                kwargs["max_tools"] = max_tools
            if isinstance(always_include, list):
                kwargs["always_include"] = [str(v) for v in always_include]

            default_complex_terms = (
                "analyze",
                "debug",
                "implement",
                "refactor",
                "compare",
                "计划",
                "分析",
                "排查",
                "实现",
                "重构",
                "测试",
                "迁移",
            )
            default_avoid_terms = (
                "你能用什么工具",
                "你会什么工具",
                "有哪些工具",
                "list tools",
                "what tools can you use",
                "tool list",
            )
            default_skip_provider_types = ("ResponsesProvider", "OpenAICodexProvider")

            selector_min_tool_count = min_tool_count if isinstance(min_tool_count, int) else 8
            selector_min_tool_count = max(1, selector_min_tool_count)
            selector_min_query_chars = min_query_chars if isinstance(min_query_chars, int) else 24
            selector_min_query_chars = max(1, selector_min_query_chars)
            selector_require_complex_query = bool(require_complex_query)

            selector_complex_terms = tuple(
                str(term).strip().lower()
                for term in (
                    complex_terms if isinstance(complex_terms, list) else list(default_complex_terms)
                )
                if str(term).strip()
            )
            selector_avoid_terms = tuple(
                str(term).strip().lower()
                for term in (
                    avoid_query_terms if isinstance(avoid_query_terms, list) else list(default_avoid_terms)
                )
                if str(term).strip()
            )
            selector_skip_provider_types = {
                str(name).strip() for name in (
                    skip_provider_types
                    if isinstance(skip_provider_types, list)
                    else list(default_skip_provider_types)
                )
                if str(name).strip()
            }
            selector_force_provider_types = {
                str(name).strip()
                for name in (force_provider_types if isinstance(force_provider_types, list) else [])
                if str(name).strip()
            }

            class _ResilientLLMToolSelectorMiddleware(LLMToolSelectorMiddleware):
                @staticmethod
                def _message_text(value: Any) -> str:
                    if isinstance(value, str):
                        return value
                    if isinstance(value, list):
                        parts: list[str] = []
                        for part in value:
                            if isinstance(part, str):
                                parts.append(part)
                                continue
                            if not isinstance(part, dict):
                                continue
                            text = part.get("text", part.get("content"))
                            if isinstance(text, str) and text.strip():
                                parts.append(text)
                        return "\n".join(parts)
                    return str(value or "")

                def _base_tool_count(self, request: Any) -> int:
                    tools = list(getattr(request, "tools", []) or [])
                    return sum(1 for tool in tools if not isinstance(tool, dict))

                def _last_user_text(self, request: Any) -> str:
                    messages = list(getattr(request, "messages", []) or [])
                    for message in reversed(messages):
                        if getattr(message, "type", "") == "human":
                            return self._message_text(getattr(message, "content", None)).strip()
                    return ""

                @staticmethod
                def _provider_class_name(request: Any) -> str:
                    model_obj = getattr(request, "model", None)
                    provider = getattr(model_obj, "provider", None)
                    return type(provider).__name__ if provider is not None else ""

                def _is_complex_query(self, text: str) -> bool:
                    if not text:
                        return False
                    lower = text.lower()
                    if len(lower) >= selector_min_query_chars:
                        return True
                    return any(term in lower for term in selector_complex_terms)

                def _should_run_selector(self, request: Any) -> bool:
                    if self._base_tool_count(request) < selector_min_tool_count:
                        return False

                    provider_class_name = self._provider_class_name(request)
                    if (
                        provider_class_name in selector_skip_provider_types
                        and provider_class_name not in selector_force_provider_types
                    ):
                        return False

                    last_user_text = self._last_user_text(request)
                    lower_text = last_user_text.lower()
                    if any(term in lower_text for term in selector_avoid_terms):
                        return False
                    if selector_require_complex_query and not self._is_complex_query(last_user_text):
                        return False
                    return True

                async def awrap_model_call(self, request, handler):
                    if not self._should_run_selector(request):
                        return await handler(request)
                    try:
                        return await super().awrap_model_call(request, handler)
                    except AssertionError as exc:
                        if "Expected dict response" not in str(exc):
                            raise
                        logger.warning(
                            "LLM tool selector fallback triggered (structured output unavailable): {}",
                            exc,
                        )
                        return await handler(request)
                    except ValueError as exc:
                        if "Model selected invalid tools" not in str(exc):
                            raise
                        logger.warning(
                            "LLM tool selector fallback triggered (invalid tool selection): {}",
                            exc,
                        )
                        return await handler(request)

            built.append(_ResilientLLMToolSelectorMiddleware(**kwargs))
            continue

        raise ValueError(
            "Unknown middleware configuration.\n"
            f"Original field: agents.defaults.middlewares[].name = {cfg.name!r}\n"
            "New supported values: prepend_system_message, tool_result_decorator, tool_result_suffix, "
            "tool_call_limit, tool_retry, model_retry, llm_tool_selector.\n"
            "Example fix: {'enabled': true, 'name': 'tool_result_decorator', 'options': {'suffix': ' [done]'}}"
        )

    return built

