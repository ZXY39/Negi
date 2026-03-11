from __future__ import annotations

import json
from typing import Any

import json_repair
from loguru import logger

from g3ku.config.schema import Config
from g3ku.org_graph.llm.provider_factory import ProviderTarget, build_provider_from_model


class OrgGraphLLM:
    def __init__(self, *, config: Config, default_model: str, reasoning_effort: str | None = None):
        self._config = config
        self._default_model = default_model
        self._reasoning_effort = reasoning_effort or config.agents.defaults.reasoning_effort
        self._targets: dict[str, ProviderTarget] = {}

    @classmethod
    def from_config(cls, config: Config, *, default_model: str) -> 'OrgGraphLLM':
        return cls(config=config, default_model=default_model, reasoning_effort=config.agents.defaults.reasoning_effort)

    def _target(self, provider_model: str | None = None) -> ProviderTarget:
        key = provider_model or self._default_model
        target = self._targets.get(key)
        if target is None:
            target = build_provider_from_model(self._config, key)
            self._targets[key] = target
        return target

    async def chat_text(self, *, system_prompt: str, user_prompt: str, provider_model: str | None = None, max_tokens: int = 1600, temperature: float = 0.2) -> str:
        target = self._target(provider_model)
        response = await target.provider.chat(
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            model=target.model_id,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=self._reasoning_effort,
        )
        content = str(response.content or '').strip()
        return content

    async def chat_json(self, *, system_prompt: str, user_prompt: str, provider_model: str | None = None, max_tokens: int = 2200, temperature: float = 0.1) -> dict[str, Any] | None:
        content = await self.chat_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            provider_model=provider_model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if not content:
            return None
        try:
            parsed = json_repair.loads(content)
        except Exception:
            parsed = self._extract_json(content)
        if isinstance(parsed, dict):
            return parsed
        logger.warning('OrgGraphLLM expected dict JSON but got {}', type(parsed).__name__)
        return None

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        candidate = text.strip()
        if '```' in candidate:
            chunks = [chunk.strip() for chunk in candidate.split('```') if chunk.strip()]
            for chunk in chunks:
                if chunk.startswith('json'):
                    chunk = chunk[4:].strip()
                try:
                    value = json.loads(chunk)
                except Exception:
                    continue
                if isinstance(value, dict):
                    return value
        try:
            value = json.loads(candidate)
        except Exception:
            return None
        return value if isinstance(value, dict) else None

