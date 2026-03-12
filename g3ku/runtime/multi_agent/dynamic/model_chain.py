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
            candidates.append((self._default_model_key(), self._loop.model_client))
            return candidates
        for target in model_chain:
            model_key = str(target.model_key or "").strip()
            if not model_key:
                continue
            candidates.append((model_key, self._build_model_client(model_key)))
        return candidates or [(self._default_model_key(), self._loop.model_client)]

    async def ainvoke_with_fallback(self, *, factory, model_chain: list[ModelFallbackTarget]):
        last_error: Exception | None = None
        for model_key, client in self.build_candidates(model_chain):
            try:
                return await factory(client, model_key)
            except Exception as exc:
                last_error = exc
                if not self._is_retryable(exc):
                    raise
                logger.warning("Dynamic subagent model fallback triggered for {}: {}", model_key, exc)
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("No model candidate available for dynamic subagent execution.")

    def _build_model_client(self, model_key: str):
        default = self._default_model_key()
        if model_key == default:
            return self._loop.model_client
        app_config = getattr(self._loop, "app_config", None)
        if app_config is None:
            return self._loop.model_client
        return build_chat_model(app_config, model_key=model_key)

    def _default_model_key(self) -> str:
        default_model_key = str(getattr(self._loop, "_runtime_default_model_key", "") or "").strip()
        if default_model_key:
            return default_model_key
        app_config = getattr(self._loop, "app_config", None)
        if app_config is not None:
            try:
                return app_config.resolve_role_model_key("ceo")
            except Exception:
                return ""
        return ""

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

