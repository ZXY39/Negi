from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from g3ku.agent.tools.base import Tool
from main.models import NodeEvidenceItem, SpawnChildResult, SpawnChildSpec, build_execution_policy_schema
from main.runtime.stage_budget import FINAL_RESULT_TOOL_NAME, STAGE_TOOL_NAME


def build_detail_level_schema(*, description: str) -> dict[str, Any]:
    return {
        'type': 'string',
        'enum': ['summary', 'full'],
        'description': str(description or '').strip() or 'Choose summary for lightweight detail or full for the complete payload.',
    }


class SubmitNextStageTool(Tool):
    def __init__(
        self,
        submit_callback: Callable[[str, int, str, list[dict[str, Any]], bool], Awaitable[dict[str, Any]]],
    ) -> None:
        self._submit_callback = submit_callback

    @property
    def name(self) -> str:
        return STAGE_TOOL_NAME

    @property
    def description(self) -> str:
        return 'Create or switch to the next stage for the current node. You must create a stage before ordinary work, and again when the current stage budget is exhausted.'

    @property
    def model_description(self) -> str:
        return 'Start the next stage for the current node.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'stage_goal': {
                    'type': 'string',
                    'description': (
                        'Concise goal for the current stage. Execution nodes should explain which work is better delegated to child nodes and which work stays local; '
                        'acceptance nodes should explain which evidence and conclusions this stage will verify.'
                    ),
                    'minLength': 1,
                },
                'tool_round_budget': {
                    'type': 'integer',
                    'description': 'How many ordinary tool rounds this stage may use. Must be between 1 and 10.',
                    'minimum': 1,
                    'maximum': 10,
                },
                'completed_stage_summary': {
                    'type': 'string',
                    'description': (
                        'Optional summary of the stage that is ending now. '
                        'Ignored when there is no active stage yet.'
                    ),
                },
                'key_refs': {
                    'type': 'array',
                    'description': (
                        'Optional stage-local canonical reference annotations for the stage that is ending now. '
                        'Ignored when there is no active stage yet.'
                    ),
                    'items': {
                        'type': 'object',
                        'properties': {
                            'ref': {
                                'type': 'string',
                                'description': 'Durable content/artifact/path reference captured during the completed stage.',
                                'minLength': 1,
                            },
                            'note': {
                                'type': 'string',
                                'description': 'Stage-local note explaining why this ref mattered in the completed stage.',
                                'minLength': 1,
                            },
                        },
                        'required': ['ref', 'note'],
                    },
                },
                'final': {
                    'type': 'boolean',
                    'description': (
                        'Optional final convergence stage flag. Use true only when this next stage should finish synthesis '
                        'with the existing evidence and must not spawn child nodes.'
                    ),
                },
            },
            'required': ['stage_goal', 'tool_round_budget'],
        }

    @property
    def model_parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'stage_goal': {
                    'type': 'string',
                    'description': 'Goal for the next stage.',
                },
                'tool_round_budget': {
                    'type': 'integer',
                    'description': 'Allowed ordinary tool calls for this stage.',
                },
                'completed_stage_summary': {
                    'type': 'string',
                    'description': 'Optional summary of the stage ending now.',
                },
                'key_refs': {
                    'type': 'array',
                    'description': 'Optional refs from the stage ending now.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'ref': {'type': 'string'},
                            'note': {'type': 'string'},
                        },
                        'required': ['ref', 'note'],
                    },
                },
                'final': {
                    'type': 'boolean',
                    'description': 'Whether the next stage is the final convergence stage.',
                },
            },
            'required': ['stage_goal', 'tool_round_budget'],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        for index, item in enumerate(list((params or {}).get('key_refs') or [])):
            if not isinstance(item, dict):
                errors.append(f'key_refs[{index}] must be an object')
                continue
            if not str(item.get('ref') or '').strip():
                errors.append(f'key_refs[{index}].ref must not be empty')
            if not str(item.get('note') or '').strip():
                errors.append(f'key_refs[{index}].note must not be empty')
        return errors

    async def execute(
        self,
        stage_goal: str,
        tool_round_budget: int,
        completed_stage_summary: str = '',
        key_refs: list[dict[str, Any]] | None = None,
        final: bool = False,
        **kwargs: Any,
    ) -> str:
        _ = kwargs
        result = await self._submit_callback(
            str(stage_goal or '').strip(),
            int(tool_round_budget or 0),
            str(completed_stage_summary or '').strip(),
            [dict(item) for item in list(key_refs or []) if isinstance(item, dict)],
            bool(final),
        )
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
        return 'Create child nodes and run them with runtime-controlled concurrency.'

    @property
    def model_description(self) -> str:
        return 'Create child nodes for delegated work.'

    @property
    def parameters(self) -> dict[str, Any]:
        child_schema = {
            'type': 'object',
            'properties': {
                'goal': {
                    'type': 'string',
                    'description': 'Goal for the child node.',
                },
                'prompt': {
                    'type': 'string',
                    'description': (
                        'Prompt for the child node. Pass file paths, directory paths, artifact/content references, search clues, and delivery expectations only; '
                        'do not inline large source bodies.'
                    ),
                },
                'execution_policy': build_execution_policy_schema(
                    description=(
                        'Execution strategy for the child node. Choose it based on the child goal itself rather than the parent node: '
                        '`focus` means only the highest-value, strictly necessary actions for the goal; '
                        '`coverage` means still start with the highest-value actions, then expand scope when the child goal explicitly needs broader coverage.'
                    ),
                ),
                'requires_acceptance': {
                    'type': 'boolean',
                    'description': (
                        'Whether this child should get a follow-up acceptance node. Use true only when the child scope is broad, costly to get wrong, '
                        'or needs a consistency pass before the parent can trust it.'
                    ),
                },
                'acceptance_prompt': {
                    'type': 'string',
                    'description': 'Prompt for the acceptance node. Required only when requires_acceptance=true.',
                },
            },
            'required': ['goal', 'prompt', 'execution_policy'],
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

    @property
    def model_parameters(self) -> dict[str, Any]:
        child_schema = {
            'type': 'object',
            'properties': {
                'goal': {'type': 'string'},
                'prompt': {'type': 'string'},
                'execution_policy': build_execution_policy_schema(
                    description='How broadly the child should explore the task.',
                ),
                'requires_acceptance': {'type': 'boolean'},
                'acceptance_prompt': {'type': 'string'},
            },
            'required': ['goal', 'prompt', 'execution_policy'],
        }
        return {
            'type': 'object',
            'properties': {
                'children': {
                    'type': 'array',
                    'items': child_schema,
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


class SubmitFinalResultTool(Tool):
    def __init__(
        self,
        submit_callback: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        *,
        node_kind: str,
    ) -> None:
        self._submit_callback = submit_callback
        self._node_kind = str(node_kind or '').strip().lower() or 'execution'

    @property
    def name(self) -> str:
        return FINAL_RESULT_TOOL_NAME

    @property
    def description(self) -> str:
        if self._node_kind == 'acceptance':
            return (
                'Submit the final structured acceptance result for the current node. '
                'Use this only when you are ready to end the node, and make it the only tool call in the turn.'
            )
        return (
            'Submit the final structured result for the current execution node. '
            'Use this only when you are ready to end the node, and make it the only tool call in the turn.'
        )

    @property
    def model_description(self) -> str:
        if self._node_kind == 'acceptance':
            return 'End the node with the final acceptance result.'
        return 'End the node with the final result.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'status': {
                    'type': 'string',
                    'enum': ['success', 'failed'],
                    'description': 'Whether the node completed successfully or failed.',
                },
                'delivery_status': {
                    'type': 'string',
                    'enum': ['final', 'blocked'],
                    'description': 'Use final for completed delivery or explicit rejection; use blocked only for genuine blockers.',
                },
                'summary': {
                    'type': 'string',
                    'description': 'Short conclusion for the node result.',
                    'minLength': 1,
                },
                'answer': {
                    'type': 'string',
                    'description': 'Final answer body for the node.',
                },
                'evidence': {
                    'type': 'array',
                    'description': 'Structured evidence supporting the submitted result.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'kind': {'type': 'string', 'enum': ['file', 'artifact', 'url']},
                            'path': {'type': 'string'},
                            'ref': {'type': 'string'},
                            'start_line': {'type': 'integer', 'minimum': 1},
                            'end_line': {'type': 'integer', 'minimum': 1},
                            'note': {'type': 'string'},
                        },
                        'required': ['kind'],
                    },
                },
                'remaining_work': {
                    'type': 'array',
                    'description': 'Remaining work items. Must be empty on success.',
                    'items': {'type': 'string'},
                },
                'blocking_reason': {
                    'type': 'string',
                    'description': 'Blocking reason. Must be empty on success.',
                },
            },
            'required': [
                'status',
                'delivery_status',
                'summary',
                'answer',
                'evidence',
                'remaining_work',
                'blocking_reason',
            ],
        }

    @property
    def model_parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'status': {
                    'type': 'string',
                    'enum': ['success', 'failed'],
                },
                'delivery_status': {
                    'type': 'string',
                    'enum': ['final', 'blocked'],
                },
                'summary': {'type': 'string'},
                'answer': {'type': 'string'},
                'evidence': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'kind': {'type': 'string', 'enum': ['file', 'artifact', 'url']},
                            'path': {'type': 'string'},
                            'ref': {'type': 'string'},
                            'start_line': {'type': 'integer'},
                            'end_line': {'type': 'integer'},
                            'note': {'type': 'string'},
                        },
                        'required': ['kind'],
                    },
                },
                'remaining_work': {
                    'type': 'array',
                    'items': {'type': 'string'},
                },
                'blocking_reason': {'type': 'string'},
            },
            'required': [
                'status',
                'delivery_status',
                'summary',
                'answer',
                'evidence',
                'remaining_work',
                'blocking_reason',
            ],
        }

    async def execute(
        self,
        status: str,
        delivery_status: str,
        summary: str,
        answer: str,
        evidence: list[dict[str, Any]],
        remaining_work: list[str],
        blocking_reason: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = kwargs
        payload = {
            'status': str(status or '').strip().lower(),
            'delivery_status': str(delivery_status or '').strip().lower(),
            'summary': str(summary or '').strip(),
            'answer': str(answer or ''),
            'evidence': [
                NodeEvidenceItem.model_validate(item).model_dump(mode='json')
                for item in list(evidence or [])
                if isinstance(item, dict)
            ],
            'remaining_work': [str(item or '').strip() for item in list(remaining_work or [])],
            'blocking_reason': str(blocking_reason or '').strip(),
        }
        return await self._submit_callback(payload)
