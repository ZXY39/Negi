from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from g3ku.agent.tools.base import Tool
from main.models import SpawnChildResult, SpawnChildSpec


class SubmitNextStageTool(Tool):
    def __init__(
        self,
        submit_callback: Callable[[str, int], Awaitable[dict[str, Any]]],
    ) -> None:
        self._submit_callback = submit_callback

    @property
    def name(self) -> str:
        return 'submit_next_stage'

    @property
    def description(self) -> str:
        return '创建或切换到下一个执行阶段。开始工作前必须先创建阶段；当当前阶段预算耗尽时，也必须先调用它。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'stage_goal': {
                    'type': 'string',
                    'description': '当前阶段的简短目标，必须写清楚哪些工作优先派生子节点，哪些工作由当前节点自行完成。',
                    'minLength': 1,
                },
                'tool_round_budget': {
                    'type': 'integer',
                    'description': '当前阶段允许使用的普通工具轮次数，范围 1 到 10。',
                    'minimum': 1,
                    'maximum': 10,
                },
            },
            'required': ['stage_goal', 'tool_round_budget'],
        }

    async def execute(self, stage_goal: str, tool_round_budget: int, **kwargs: Any) -> str:
        result = await self._submit_callback(str(stage_goal or '').strip(), int(tool_round_budget or 0))
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


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
        return json.dumps(
            {'children': [item.model_dump(mode='json', exclude_none=True) for item in results]},
            ensure_ascii=False,
        )
