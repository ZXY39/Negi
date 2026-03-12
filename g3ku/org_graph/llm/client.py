from __future__ import annotations

import json
from typing import Any

import json_repair
from loguru import logger

from g3ku.config.schema import Config
from g3ku.org_graph.errors import ModelChainUnavailableError
from g3ku.org_graph.llm.provider_factory import ProviderTarget, build_provider_from_model_key
from g3ku.providers.fallback import is_retryable_model_error, response_requires_retry


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
        key = str(provider_model or self._default_model or "").strip()
        target = self._targets.get(key)
        if target is None:
            target = build_provider_from_model_key(self._config, key)
            self._targets[key] = target
        return target

    def _targets_for(
        self,
        *,
        provider_model: str | None = None,
        provider_model_chain: list[str] | None = None,
    ) -> list[ProviderTarget]:
        refs = [str(item or "").strip() for item in (provider_model_chain or []) if str(item or "").strip()]
        if not refs:
            refs = [str(provider_model or self._default_model or "").strip()]
        targets: list[ProviderTarget] = []
        for ref in refs:
            try:
                targets.append(self._target(ref))
            except Exception:
                if len(refs) == 1:
                    raise
                logger.warning("OrgGraphLLM candidate unavailable for {} - trying next", ref)
        return targets

    async def chat_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        provider_model: str | None = None,
        provider_model_chain: list[str] | None = None,
        max_tokens: int = 1600,
        temperature: float = 0.2,
        monitor_context: dict[str, Any] | None = None,
    ) -> str:
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ]
        self._record_monitor_input(monitor_context, system_prompt=system_prompt, user_prompt=user_prompt)
        targets = self._targets_for(provider_model=provider_model, provider_model_chain=provider_model_chain)
        last_error: Exception | None = None
        last_content = ""
        retryable_unavailable = False
        for index, target in enumerate(targets):
            effective_max_tokens = max(1, min(int(max_tokens), int(target.max_tokens_limit))) if target.max_tokens_limit else max(1, int(max_tokens))
            effective_temperature = float(target.default_temperature) if target.default_temperature is not None else float(temperature)
            effective_reasoning = target.default_reasoning_effort or self._reasoning_effort
            try:
                response = await target.provider.chat(
                    messages=messages,
                    model=target.model_id,
                    max_tokens=effective_max_tokens,
                    temperature=effective_temperature,
                    reasoning_effort=effective_reasoning,
                )
            except Exception as exc:
                last_error = exc
                retryable_unavailable = is_retryable_model_error(exc, retry_on=target.retry_on)
                if index < len(targets) - 1 and is_retryable_model_error(exc, retry_on=target.retry_on):
                    logger.warning("OrgGraphLLM fallback triggered for {}: {}", target.provider_ref, exc)
                    continue
                if retryable_unavailable:
                    raise ModelChainUnavailableError(str(exc)) from exc
                raise

            content = str(response.content or '').strip()
            last_content = content
            if index < len(targets) - 1 and response_requires_retry(response, retry_on=target.retry_on):
                retryable_unavailable = True
                logger.warning("OrgGraphLLM fallback triggered for {}: {}", target.provider_ref, content or response.finish_reason)
                continue
            self._record_monitor_output(monitor_context, content)
            return content

        if last_error is not None:
            if retryable_unavailable:
                raise ModelChainUnavailableError(str(last_error)) from last_error
            raise last_error
        if retryable_unavailable:
            raise ModelChainUnavailableError(last_content or 'Model chain temporarily unavailable')
        self._record_monitor_output(monitor_context, last_content)
        return last_content

    async def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        provider_model: str | None = None,
        provider_model_chain: list[str] | None = None,
        max_tokens: int = 2200,
        temperature: float = 0.1,
        monitor_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        content = await self.chat_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            provider_model=provider_model,
            provider_model_chain=provider_model_chain,
            max_tokens=max_tokens,
            temperature=temperature,
            monitor_context=monitor_context,
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

    @staticmethod
    def _record_monitor_input(monitor_context: dict[str, Any] | None, *, system_prompt: str, user_prompt: str) -> None:
        ctx = monitor_context if isinstance(monitor_context, dict) else {}
        service = ctx.get('service')
        project = ctx.get('project')
        unit = ctx.get('unit')
        if service is None or project is None or unit is None:
            return
        stage_id = ctx.get('stage_id')
        kind = str(ctx.get('input_kind') or 'input')
        content = json.dumps({'system_prompt': system_prompt, 'user_prompt': user_prompt}, ensure_ascii=False, indent=2)
        service.monitor_service.record_input(project=project, unit=unit, content=content, stage_id=stage_id, kind=kind, meta={'source': 'org_graph_llm'})

    @staticmethod
    def _record_monitor_output(monitor_context: dict[str, Any] | None, content: str) -> None:
        ctx = monitor_context if isinstance(monitor_context, dict) else {}
        service = ctx.get('service')
        project = ctx.get('project')
        unit = ctx.get('unit')
        if service is None or project is None or unit is None:
            return
        stage_id = ctx.get('stage_id')
        kind = str(ctx.get('output_kind') or 'output')
        service.monitor_service.record_output(project=project, unit=unit, content=str(content or ''), stage_id=stage_id, kind=kind, meta={'source': 'org_graph_llm'})
