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
        return '并发创建多个子节点；每个成功子节点都会自动触发对应的验收节点。'

    @property
    def parameters(self) -> dict[str, Any]:
        child_schema = {
            'type': 'object',
            'properties': {
                'goal': {'type': 'string', 'description': '子节点目标。'},
                'prompt': {'type': 'string', 'description': '发送给子节点执行大模型的提示词。'},
                'acceptance_prompt': {'type': 'string', 'description': '发送给验收节点的验收提示词。'},
            },
            'required': ['goal', 'prompt', 'acceptance_prompt'],
        }
        return {
            'type': 'object',
            'properties': {
                'children': {'type': 'array', 'items': child_schema, 'minItems': 1},
            },
            'required': ['children'],
        }

    async def execute(self, children: list[dict[str, Any]], __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        specs = [SpawnChildSpec.model_validate(item) for item in list(children or [])]
        results = await self._spawn_callback(specs, runtime.get('current_tool_call_id'))
        return json.dumps({'children': [item.model_dump(mode='json') for item in results]}, ensure_ascii=False)
