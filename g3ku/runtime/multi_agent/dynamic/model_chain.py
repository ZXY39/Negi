from __future__ import annotations

from typing import Any

from loguru import logger

from g3ku.providers.chatmodels import build_chat_model
from g3ku.runtime.multi_agent.dynamic.types import ModelFallbackTarget


class ModelChainExecutor:
    def __init__(self, *, loop) -> None:
        self._loop = loop

    def build_candidates(self, model_chain: list[ModelFallbackTarget]) -> list[tuple[str, Any]]:
        candidates: list[tuple[str, Any]] = []
        if not model_chain:
            candidates.append((self._default_provider_model(), self._loop.model_client))
            return candidates
        for target in model_chain:
            provider_model = str(target.provider_model or "").strip()
            if not provider_model:
                continue
            candidates.append((provider_model, self._build_model_client(provider_model)))
        return candidates or [(self._default_provider_model(), self._loop.model_client)]

    async def ainvoke_with_fallback(self, *, factory, model_chain: list[ModelFallbackTarget]):
        last_error: Exception | None = None
        for provider_model, client in self.build_candidates(model_chain):
            try:
                return await factory(client, provider_model)
            except Exception as exc:
                last_error = exc
                if not self._is_retryable(exc):
                    raise
                logger.warning("Dynamic subagent model fallback triggered for {}: {}", provider_model, exc)
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("No model candidate available for dynamic subagent execution.")

    def _build_model_client(self, provider_model: str):
        default = self._default_provider_model()
        if provider_model == default:
            return self._loop.model_client
        app_config = getattr(self._loop, "app_config", None)
        if app_config is None:
            return self._loop.model_client
        copied = app_config.model_copy(deep=True)
        copied.agents.defaults.model = provider_model
        return build_chat_model(copied)

    def _default_provider_model(self) -> str:
        provider_name = str(getattr(self._loop, "provider_name", "") or "").strip()
        model_name = str(getattr(self._loop, "model", "") or "").strip()
        return f"{provider_name}:{model_name}" if provider_name and model_name else model_name

    @classmethod
    def _is_retryable(cls, exc: Exception) -> bool:
        text = cls._exception_chain_text(exc)
        if any(token in text for token in [
            "sqlite",
            "database",
            "cursor",
            "checkpointer",
            "aiosqlite",
            "programmingerror",
            "no active connection",
            "cannot operate on a closed database",
        ]):
            return False
        retry_tokens = [
            "429",
            "502",
            "503",
            "504",
            "5xx",
            "timeout",
            "timed out",
            "network error",
            "connecterror",
            "connect error",
            "all connection attempts failed",
            "connection reset",
            "connection refused",
            "temporar",
            "rate limit",
            "remoteprotocolerror",
            "readerror",
            "sslerror",
        ]
        return any(token in text for token in retry_tokens)

    @staticmethod
    def _exception_chain_text(exc: Exception) -> str:
        parts: list[str] = []
        seen: set[int] = set()
        stack: list[BaseException] = [exc]
        while stack:
            current = stack.pop(0)
            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)
            parts.append(f"{type(current).__name__}: {current}")
            cause = getattr(current, "__cause__", None)
            context = getattr(current, "__context__", None)
            if cause is not None:
                stack.append(cause)
            if context is not None:
                stack.append(context)
        return " | ".join(parts).lower()

