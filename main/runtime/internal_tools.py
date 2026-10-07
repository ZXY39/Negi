from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from g3ku.agent.tools.base import Tool
from main.models import NodeEvidenceItem, SpawnChildResult, SpawnChildSpec, build_execution_policy_schema
from main.runtime.stage_budget import (
    FINAL_RESULT_TOOL_NAME,
    SILENT_TOOL_NAME,
    STAGE_TOOL_NAME,
    STAGE_TOOL_ROUND_BUDGET_MAX,
    STAGE_TOOL_ROUND_BUDGET_MIN,
)


def build_detail_level_schema(*, description: str) -> dict[str, Any]:
    return {
        'type': 'string',
        'enum': ['summary', 'full'],
        'description': str(description or '').strip() or 'Choose summary for lightweight detail or full for the complete payload.',
    }


def build_keep_contract_list_schema(*, description: str) -> dict[str, Any]:
    """`keep_tools` / `keep_skills` 的共用形状。

    两条车道三份 schema 由同一个构造函数产出，`parameters` 与 `model_parameters` 只有
    description 不同：约束词表必须逐字镜像（`test_control_tool_model_schema_parity.py`），
    而字段级 description 在出 provider 前会被 `sanitize_provider_parameters_schema` 整体
    剥掉，所以真正的语义只写在工具级 `model_description` 与三份提示词里。
    """
    return {
        'type': 'array',
        'description': str(description or '').strip(),
        'items': {'type': 'string', 'minLength': 1},
    }


def normalize_keep_contract_names(value: Any) -> list[str]:
    """`keep_tools` / `keep_skills` 的取值口径：去空、去重、保序。

    三条车道（执行 / 验收 / CEO 前门）与两个提交落点共用这一份，名字列表在这里定形后
    才进账本，所以「模型交的名字」与「回执点名的名字」永远同形。
    """
    collected: list[str] = []
    seen: set[str] = set()
    for raw in list(value or []):
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        collected.append(name)
    return collected


KEEP_CONTRACT_DROP_GATE_ERROR = (
    'keep_tools / keep_skills require drop_completed_stage_tool_detail=true in the same call: '
    'without the drop nothing leaves the context, so there is no stage block to keep a contract in. '
    'Either set drop_completed_stage_tool_detail=true with a non-empty completed_stage_summary, or '
    'send the raw rows on and drop these names'
)


def keep_contracts_require_drop_error(keep_tools: Any, keep_skills: Any) -> str:
    """`keep_*` 只在裁撤为真时才有意义——判据只有一份。

    工具层的 `validate_params` 与两条车道的提交落点（`log_service.submit_next_stage` /
    `_submit_frontdoor_next_stage_state`）都调它，与 `drop requires non-empty
    completed_stage_summary` 同一口径：绕过工具层的写入者也造不出「参数被静默吞掉」的态，
    因为不裁撤时这个名字根本无处可写。
    """
    if not (normalize_keep_contract_names(keep_tools) or normalize_keep_contract_names(keep_skills)):
        return ''
    return KEEP_CONTRACT_DROP_GATE_ERROR


class SubmitNextStageTool(Tool):
    hide_universal_timeout_parameter = True
    # 重跑会被 log_service.submit_next_stage 的状态闸门拒掉：刚开的活动阶段没有
    # 实质进展时第二次调用直接报错，而不是把阶段再往前推一格。
    rerun_safe = True

    def __init__(
        self,
        submit_callback: Callable[..., Awaitable[dict[str, Any]]],
    ) -> None:
        self._submit_callback = submit_callback

    @property
    def name(self) -> str:
        return STAGE_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'Create or switch to the next stage. Open a stage before ordinary work, and again '
            'when the current stage budget is exhausted. When there is no active stage and you need '
            'to use tools, submit this together with those tools in the same batch — it runs first, '
            'then the tools are booked on the first round of the new stage.'
        )

    @property
    def model_description(self) -> str:
        return (
            'Start the next stage for the current node. '
            'keep_tools / keep_skills only apply together with drop_completed_stage_tool_detail=true.'
        )

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
                    'description': (
                        f'How many ordinary tool rounds this stage may use. '
                        f'Must not exceed {STAGE_TOOL_ROUND_BUDGET_MAX}; plan for at least '
                        f'{STAGE_TOOL_ROUND_BUDGET_MIN}. A smaller value is raised to '
                        f'{STAGE_TOOL_ROUND_BUDGET_MIN} instead of failing.'
                    ),
                    'maximum': STAGE_TOOL_ROUND_BUDGET_MAX,
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
                'drop_completed_stage_tool_detail': {
                    'type': 'boolean',
                    'description': (
                        'Set true to move the stage you are closing out of the model context: its raw '
                        'tool arguments and outputs stop being sent and only completed_stage_summary '
                        'remains, as the stage block. Requires a non-empty completed_stage_summary in '
                        'this same call. Nothing is deleted — the stage keeps its full round-by-round '
                        'record in the durable ledger. Leave false (default) when the next stage must '
                        'still see the exact arguments or output text of this one.'
                    ),
                },
                'keep_tools': build_keep_contract_list_schema(
                    description=(
                        'Names of hydrated tools whose contract body this closing stage still grants the '
                        'ability to call. Only read when drop_completed_stage_tool_detail is true; with '
                        'drop false the call is rejected rather than silently ignoring these names. Each '
                        'name must be a tool this stage loaded via load_tool_context. A tool you do not '
                        'name loses its contract with the raw rows: it leaves the callable list and goes '
                        'back to the candidate pool, and you must load it again to call it.'
                    ),
                ),
                'keep_skills': build_keep_contract_list_schema(
                    description=(
                        'Names of skills whose workflow body you want kept next to the stage block after '
                        'dropping this stage\'s raw rows. Only read when drop_completed_stage_tool_detail '
                        'is true. Each name must be a skill this stage loaded via load_skill_context. '
                        'Skills are never hydrated, so keeping one changes no callable list — it only '
                        'keeps the text in context.'
                    ),
                ),
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
                    'minLength': 1,
                },
                'tool_round_budget': {
                    'type': 'integer',
                    'description': (
                        f'Allowed ordinary tool calls for this stage. Plan for {STAGE_TOOL_ROUND_BUDGET_MIN} to '
                        f'{STAGE_TOOL_ROUND_BUDGET_MAX}; a value under {STAGE_TOOL_ROUND_BUDGET_MIN} is raised to '
                        f'{STAGE_TOOL_ROUND_BUDGET_MIN}, one over {STAGE_TOOL_ROUND_BUDGET_MAX} fails.'
                    ),
                    'maximum': STAGE_TOOL_ROUND_BUDGET_MAX,
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
                            'ref': {'type': 'string', 'minLength': 1},
                            'note': {'type': 'string', 'minLength': 1},
                        },
                        'required': ['ref', 'note'],
                    },
                },
                'drop_completed_stage_tool_detail': {
                    'type': 'boolean',
                    'description': (
                        'Drop the closing stage\'s raw tool arguments/outputs from context, keeping '
                        'completed_stage_summary. Requires that summary to be non-empty. Nothing '
                        'expires on a count any more: a stage you leave false keeps sending its tool '
                        'rows until summary compression replaces that history.'
                    ),
                },
                'keep_tools': build_keep_contract_list_schema(
                    description='Hydrated tools this closing stage loaded and must stay callable after the drop.',
                ),
                'keep_skills': build_keep_contract_list_schema(
                    description='Skills this closing stage loaded and whose body must stay in context after the drop.',
                ),
            },
            'required': ['stage_goal', 'tool_round_budget'],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        source = params or {}
        for index, item in enumerate(list(source.get('key_refs') or [])):
            if not isinstance(item, dict):
                errors.append(f'key_refs[{index}] must be an object')
                continue
            if not str(item.get('ref') or '').strip():
                errors.append(f'key_refs[{index}].ref must not be empty')
            if not str(item.get('note') or '').strip():
                errors.append(f'key_refs[{index}].note must not be empty')
        if bool(source.get('drop_completed_stage_tool_detail')) and not str(
            source.get('completed_stage_summary') or ''
        ).strip():
            # 裁撤的唯一保险就是同批那条总结：没有它，移出上下文等于把该阶段唯一的
            # 记录一起移走（库里 46.2% 的终态阶段总结为空，这不是假想情况）。
            errors.append(
                'drop_completed_stage_tool_detail requires a non-empty completed_stage_summary '
                'in the same call'
            )
        # keep_* 挂在不裁撤的提交上是一条空承诺：正文本来就在上下文里，没有块可以放它，
        # 收下名字会让模型以为「留住了」而下一跳发现工具仍在 callable 是理所当然。
        drop_gate_error = keep_contracts_require_drop_error(
            source.get('keep_tools'),
            source.get('keep_skills'),
        )
        if drop_gate_error and not bool(source.get('drop_completed_stage_tool_detail')):
            errors.append(drop_gate_error)
        for field in ('keep_tools', 'keep_skills'):
            value = source.get(field)
            if value is None:
                continue
            if not isinstance(value, list):
                errors.append(f'{field} must be an array of names')
                continue
            for index, item in enumerate(value):
                if not str(item or '').strip():
                    errors.append(f'{field}[{index}] must not be empty')
        return errors

    async def execute(
        self,
        stage_goal: str,
        tool_round_budget: int,
        completed_stage_summary: str = '',
        key_refs: list[dict[str, Any]] | None = None,
        final: bool = False,
        drop_completed_stage_tool_detail: bool = False,
        keep_tools: list[str] | None = None,
        keep_skills: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        _ = kwargs
        normalized_keep_tools = normalize_keep_contract_names(keep_tools)
        normalized_keep_skills = normalize_keep_contract_names(keep_skills)
        callback_args: tuple[Any, ...] = (
            str(stage_goal or '').strip(),
            int(tool_round_budget or 0),
            str(completed_stage_summary or '').strip(),
            [dict(item) for item in list(key_refs or []) if isinstance(item, dict)],
            bool(final),
            bool(drop_completed_stage_tool_detail),
        )
        callback_kwargs: dict[str, Any] = {}
        if normalized_keep_tools or normalized_keep_skills:
            # 只在模型真点名保留时才多带这两个关键字：六元回调是三条车道（执行 / 验收 /
            # CEO 前门）与既有夹具共用的签名，无条件多塞会打断每一个没改过的闭包，而
            # 「没点名」那一路本来就不需要读它们。
            callback_kwargs = {
                'keep_tools': normalized_keep_tools,
                'keep_skills': normalized_keep_skills,
            }
        result = await self._submit_callback(*callback_args, **callback_kwargs)
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


class SilentTool(Tool):
    """本轮对用户保持静默的收尾信号。

    替代旧的 `[G3KU_SILENT]` / `HEARTBEAT_OK` 文案出口：那两条要求模型把整条输出
    恰好等于哨兵串，实盘 11 次尝试全部写成「正文 + 空行 + 哨兵」而静默失败。调工具
    把「不外发」变成一个与正文彼此独立的通道，正文照常进上下文，于是同一动作既是
    静默也是痕迹。参数落进转录行，审计与后续轮次都能读到判据。
    """

    hide_universal_timeout_parameter = True

    @property
    def name(self) -> str:
        return SILENT_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'End this turn without delivering a reply to the user or the channel. Your accompanying '
            'text is still kept in the conversation and visible to you on later turns; only delivery '
            'is suppressed. Call it when a later reply in this session already covered the same '
            'deliverable — name that covering reply in `superseded_by`. When the deliverable has not '
            'actually been reported yet, reply normally instead: never use this to dodge a result '
            'the user is waiting for.'
        )

    @property
    def model_description(self) -> str:
        return 'Stay silent this turn: keep the text for context, deliver nothing.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'reason': {
                    'type': 'string',
                    'description': (
                        'Why nothing is being delivered. On a user-initiated turn this doubles as '
                        'the fallback text, so it must read as something the user could be shown.'
                    ),
                    'minLength': 1,
                },
                'subject': {
                    'type': 'string',
                    'description': 'What is being silenced, usually a task or event id.',
                },
                'superseded_by': {
                    'type': 'string',
                    'description': 'Id of the later reply that already covered this deliverable.',
                },
            },
            'required': ['reason'],
        }

    @property
    def model_parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'reason': {
                    'type': 'string',
                    'description': 'Why nothing is delivered this turn.',
                    'minLength': 1,
                },
                'subject': {
                    'type': 'string',
                    'description': 'Task or event id being silenced.',
                },
                'superseded_by': {
                    'type': 'string',
                    'description': 'Id of the later reply that already covered it.',
                },
            },
            'required': ['reason'],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not str((params or {}).get('reason') or '').strip():
            errors.append('reason must not be empty')
        return errors

    async def execute(
        self,
        reason: str = '',
        subject: str = '',
        superseded_by: str = '',
        **kwargs: Any,
    ) -> str:
        _ = kwargs
        return json.dumps(
            {
                'silenced': True,
                'reason': str(reason or '').strip(),
                'subject': str(subject or '').strip(),
                'superseded_by': str(superseded_by or '').strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )


class SpawnChildNodesTool(Tool):
    hide_universal_timeout_parameter = True
    # 派生子节点工具在一次调用内跑完整个子节点流水线（含嵌套派生与验收节点），
    # 运行时长天然无界，不得套任何外层机械超时；中断只能走任务级取消链。
    exempt_universal_timeout = True
    # 重跑按 tool_call_id 命中父节点 metadata 里的 spawn_operations 记录：已完成的
    # 批次直接复用原 entries，不会再物化一批子节点。
    rerun_safe = True

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
        return (
            'Create child nodes and run them with runtime-controlled concurrency. '
            'All children in one call start concurrently with no ordering guarantee, and the call '
            'returns only after every child pipeline is terminal. A child that consumes other '
            "children's outputs (e.g. an acceptance/verification node for its siblings) must NOT "
            'share the batch: spawn it in a later call after the depended-on batch returns, or use '
            'requires_acceptance for per-child acceptance.'
        )

    @property
    def model_description(self) -> str:
        return (
            'Create child nodes for delegated work. One call = one concurrent batch (no ordering); '
            'never put a node that consumes its siblings’ outputs (e.g. acceptance) in the same '
            'batch — spawn it in a later batch, or use requires_acceptance for single-child acceptance.'
        )

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
                        'or needs a consistency pass before the parent can trust it. The acceptance node is activated only after this child pipeline '
                        'is terminal — this is the correct way to gate acceptance for a single child; do not spawn a sibling acceptance node in the '
                        'same batch for it.'
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
                    'description': (
                        'One batch of children started concurrently (no ordering guarantee); the call returns after all of them are terminal. '
                        'Put only mutually independent, ready branches in one batch. A child that depends on other children’s outputs '
                        '(e.g. an acceptance/verification/aggregation node for its siblings) must be spawned in a separate later batch.'
                    ),
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
                    'description': (
                        'One concurrent batch (no ordering). Independent ready branches only; a child that consumes '
                        'its siblings’ outputs (e.g. acceptance) must go in a separate later batch.'
                    ),
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


class SubmitMessageDistributionTool(Tool):
    hide_universal_timeout_parameter = True

    def __init__(
        self,
        submit_callback: Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]],
    ) -> None:
        self._submit_callback = submit_callback

    @property
    def name(self) -> str:
        return 'submit_message_distribution'

    @property
    def description(self) -> str:
        return (
            'Submit per-child message delivery decisions for the current distribution turn. '
            '每个子节点恰好一条 children 决策，target_node_id 必须原样取自 live_children。'
            '字段必填是条件式的：action=distribute（或 should_distribute=true）时该条的 message 必须非空'
            '（写给这个子节点的话，不是给你的理由）；action=skip/terminate 时 reason 必须非空。'
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'children': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'target_node_id': {'type': 'string'},
                            'should_distribute': {'type': 'boolean'},
                            'action': {'type': 'string', 'enum': ['distribute', 'skip', 'terminate']},
                            'message': {'type': 'string'},
                            'reason': {'type': 'string'},
                        },
                        'required': ['target_node_id', 'should_distribute', 'reason'],
                    },
                },
                'notes': {'type': 'string'},
            },
            'required': ['children'],
        }

    async def execute(
        self,
        children: list[dict[str, Any]],
        notes: str = '',
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = kwargs
        payload = {
            'children': [dict(item) for item in list(children or []) if isinstance(item, dict)],
            'notes': str(notes or '').strip(),
        }
        result = self._submit_callback(payload)
        if isinstance(result, Awaitable):
            return await result
        return dict(result or {}) if isinstance(result, dict) else payload


class SubmitNoticeInspectionDecisionTool(Tool):
    """被验收检验中的节点收到定向通知时的决策工具。

    resume_execution：通知要求更改最终输出 → 程序打断验收节点，目标节点
    带通知恢复执行；continue_acceptance：无需更改 → 验收继续，验收节点
    会收到「被检验节点收到了通知（含原文）」的告知。
    """

    hide_universal_timeout_parameter = True

    def __init__(
        self,
        submit_callback: Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]],
    ) -> None:
        self._submit_callback = submit_callback

    @property
    def name(self) -> str:
        return 'submit_notice_inspection_decision'

    @property
    def description(self) -> str:
        return 'Submit the inspection decision for a notice received while this node is under acceptance inspection.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'enum': ['resume_execution', 'continue_acceptance'],
                    'description': (
                        'resume_execution: the notice requires changing the final output; interrupt the '
                        'running acceptance inspection and re-run this node with the notice merged. '
                        'continue_acceptance: the notice does not require changing the final output; let '
                        'the acceptance inspection continue (the inspector will be informed of the notice).'
                    ),
                },
                'reason': {
                    'type': 'string',
                    'description': 'Why the notice does or does not require changing the final output.',
                },
                'notes': {'type': 'string'},
            },
            'required': ['action', 'reason'],
        }

    async def execute(
        self,
        action: str,
        reason: str = '',
        notes: str = '',
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = kwargs
        payload = {
            'action': str(action or '').strip(),
            'reason': str(reason or '').strip(),
            'notes': str(notes or '').strip(),
        }
        result = self._submit_callback(payload)
        if isinstance(result, Awaitable):
            return await result
        return dict(result or {}) if isinstance(result, dict) else payload


class SubmitFinalResultTool(Tool):
    hide_universal_timeout_parameter = True

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
                'Use this only when you are ready to end the node, and make it the only tool call in the turn. '
                'For a repairable rejection, use failed + delivery_status=final; the runtime returns revised execution output for another acceptance pass. For an execution anomaly that must not be retried, use failed + delivery_status=blocked and explain why in blocking_reason.'
            )
        return (
            'Submit the final structured result for the current execution node. '
            'Use this only when you are ready to end the node, and make it the only tool call in the turn. '
            'If acceptance rejects the submission, the tool returns the rejection feedback and you must continue from that feedback.'
        )

    @property
    def model_description(self) -> str:
        if self._node_kind == 'acceptance':
            return 'Submit the current acceptance result; failed+final requests repair, while failed+blocked records an execution anomaly as terminal without retry. For a repairable failed+final verdict, send blocking_reason as an empty string: the inspected execution node reads summary plus remaining_work as its repair instructions, and nothing else you write into blocking_reason.'
        return 'Submit the current result; rejection returns acceptance feedback instead of ending the node.'

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
                    'description': 'Use final for success or a repairable rejection; use blocked for an execution anomaly that must not be retried or a genuine blocker.',
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
                'summary': {'type': 'string', 'minLength': 1},
                'answer': {'type': 'string'},
                'evidence': {
                    'type': 'array',
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
