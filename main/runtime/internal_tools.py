from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from g3ku.agent.tools.base import Tool
from main.models import SpawnChildResult, SpawnChildSpec


SPAWN_PRECHECK_TOOL_NAME = 'spawn_precheck'
SPAWN_CHILD_NODES_TOOL_NAME = 'spawn_child_nodes'


class SpawnPrecheckTool(Tool):
    @property
    def name(self) -> str:
        return SPAWN_PRECHECK_TOOL_NAME

    @property
    def description(self) -> str:
        return '在本轮调用真实工具前，先声明是否需要派生子节点及原因。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'decision': {
                    'type': 'string',
                    'enum': ['spawn_child_nodes', 'continue_self_execute'],
                    'description': '本轮选择派生子节点，还是继续自行执行。',
                },
                'reason': {
                    'type': 'string',
                    'minLength': 1,
                    'description': '本轮为什么做出该选择。',
                },
                'rule_ids': {
                    'type': 'array',
                    'items': {
                        'type': 'integer',
                        'enum': [1, 2, 3, 4],
                    },
                    'minItems': 1,
                    'description': '本轮命中或不命中的派生规则编号。',
                },
                'rule_semantics': {
                    'type': 'string',
                    'enum': ['matched', 'unmatched'],
                    'description': 'rule_ids 表示命中的规则还是未命中的规则。',
                },
            },
            'required': ['decision', 'reason', 'rule_ids', 'rule_semantics'],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        rule_ids = params.get('rule_ids') if isinstance(params, dict) else None
        if not isinstance(rule_ids, list) or not rule_ids:
            errors.append('rule_ids must contain at least one item')
        decision = str((params or {}).get('decision') or '').strip()
        semantics = str((params or {}).get('rule_semantics') or '').strip()
        if decision == 'spawn_child_nodes' and semantics and semantics != 'matched':
            errors.append('rule_semantics must be matched when decision=spawn_child_nodes')
        if decision == 'continue_self_execute' and semantics and semantics != 'unmatched':
            errors.append('rule_semantics must be unmatched when decision=continue_self_execute')
        return errors

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return json.dumps(
            {
                'status': 'internal_only',
                'tool_name': self.name,
                'summary': 'spawn_precheck must be consumed by the runtime and should not execute as a real tool',
            },
            ensure_ascii=False,
        )


class SpawnChildNodesTool(Tool):
    def __init__(
        self,
        spawn_callback: Callable[[list[SpawnChildSpec], str | None], Awaitable[list[SpawnChildResult]]],
    ) -> None:
        self._spawn_callback = spawn_callback

    @property
    def name(self) -> str:
        return SPAWN_CHILD_NODES_TOOL_NAME

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
