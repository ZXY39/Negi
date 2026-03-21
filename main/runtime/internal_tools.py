from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from g3ku.agent.tools.base import Tool
from main.models import SpawnChildResult, SpawnChildSpec


class SpawnChildNodesTool(Tool):
    def __init__(
        self,
        spawn_callback: Callable[[list[SpawnChildSpec], str | None], Awaitable[list[SpawnChildResult]]],
    ) -> None:
        self._spawn_callback = spawn_callback

    @property
    def name(self) -> str:
        return 'spawn_child_nodes'

    @property
    def description(self) -> str:
        return '并发创建多个子节点。'

    @property
    def parameters(self) -> dict[str, Any]:
        child_schema = {
            'type': 'object',
            'properties': {
                'goal': {
                    'type': 'string',
                    'description': '子节点目标。',
                },
                'prompt': {
                    'type': 'string',
                    'description': '发送给子节点执行模型的提示词。只传文件路径、目录路径、artifact/content 引用、搜索线索和交付要求，不要直接内联待读正文。',
                },
                'requires_acceptance': {
                    'type': 'boolean',
                    'description': '是否需要为该子节点追加验收节点。仅在范围广、复杂度高，出错代价大，或需要一致性复核时设为 true。',
                },
                'acceptance_prompt': {
                    'type': 'string',
                    'description': '发送给验收节点的提示词。仅当 requires_acceptance=true 时必填，用于说明验收标准。',
                },
            },
            'required': ['goal', 'prompt'],
        }
        return {
            'type': 'object',
            'properties': {
                'children': {
                    'type': 'array',
                    'items': child_schema,
                    'minItems': 1,
                },
            },
            'required': ['children'],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        for index, item in enumerate(list((params or {}).get('children') or [])):
            if not isinstance(item, dict):
                continue
            requires_acceptance = item.get('requires_acceptance')
            acceptance_prompt = str(item.get('acceptance_prompt') or '').strip()
            if requires_acceptance is True and not acceptance_prompt:
                errors.append(f'children[{index}].acceptance_prompt is required when requires_acceptance=true')
        return errors

    async def execute(self, children: list[dict[str, Any]], __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        specs = [SpawnChildSpec.model_validate(item) for item in list(children or [])]
        results = await self._spawn_callback(specs, runtime.get('current_tool_call_id'))
        return json.dumps({'children': [item.model_dump(mode='json') for item in results]}, ensure_ascii=False)
