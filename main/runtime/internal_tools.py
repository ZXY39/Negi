from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from g3ku.agent.tools.base import Tool
from main.models import (
    NodeEvidenceItem,
    SpawnChildResult,
    SpawnChildSpec,
    build_execution_policy_schema,
)
from main.runtime.tool_names import (
    ENTER_WAIT_TOOL_NAME,
    FINAL_RESULT_TOOL_NAME,
    SEND_MESSAGE_TOOL_NAME,
    SILENT_TOOL_NAME,
)


def build_detail_level_schema(*, description: str) -> dict[str, Any]:
    return {
        'type': 'string',
        'enum': ['summary', 'full'],
        'description': str(description or '').strip() or 'Choose summary for lightweight detail or full for the complete payload.',
    }


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


class EnterWaitTool(Tool):
    """主动停靠，等一条消息把自己叫醒。

    这是节点"现在没活可干、但在等某件事"的唯一表达方式：调用它之后本回合就结束，
    节点进入等待，直到信箱里出现满足 `wake_on` 的未读消息才被唤醒。

    防丢唤醒：调用时若已经有满足条件的未读，不会入睡——未读立即注入、回合继续，
    工具回执说明"有未读消息，未进入等待"。这条把"最后一条完成消息在入睡前到达"
    的竞态从机制上消灭，因此不需要调用方先查一遍信箱。
    """

    hide_universal_timeout_parameter = True

    def __init__(self, park: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self._park = park

    @property
    def name(self) -> str:
        return ENTER_WAIT_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'Stop this turn and wait until a message wakes you. Use `all_children` when you have '
            'finished your own work and only the children\'s results are outstanding: it wakes you '
            'once every direct execution child has settled and its terminal message is already in '
            'your mailbox, so you are not woken once per child. Use `any` when any single message '
            'should bring you back. If qualifying mail is already waiting, this does not park you — '
            'it injects that mail and the turn continues. `note` is echoed back to you on wake so '
            'you remember what you were waiting for. Do not call this while you still have work you '
            'can do now.'
        )

    @property
    def model_description(self) -> str:
        return 'Park until a message wakes you. all_children waits for every direct child to report.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'wake_on': {
                    'type': 'string',
                    'enum': ['any', 'all_children'],
                    'description': (
                        'any = first qualifying message wakes you; all_children = every direct '
                        'execution child settled and reported.'
                    ),
                },
                'note': {
                    'type': 'string',
                    'description': 'What you are waiting for. Echoed back when you wake.',
                },
            },
            'required': [],
        }

    async def execute(self, wake_on: str = 'any', note: str = '', **kwargs: Any) -> str:
        _ = kwargs
        result = await self._park(
            wake_on=str(wake_on or '').strip() or 'any',
            note=str(note or '').strip(),
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


class SendMessageTool(Tool):
    """给父节点、直接子节点、或（仅根节点）前门 agent 发一条消息。

    一次可发给多个直接子节点；投递逐目标独立判定。`intent_kind=请求` 表示发完就要
    停靠等回应，因此收件人会被立即唤醒；`进展` / `指令` 只在收件人自己的等待策略
    允许时才唤醒它。

    工具本身不做寻址判定：合法目标集合与父/子关系由运行时的投递入口算，越址时
    回执里会带上可用的地址，避免模型只能靠猜。
    """

    hide_universal_timeout_parameter = True

    def __init__(self, deliver: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self._deliver = deliver

    @property
    def name(self) -> str:
        return SEND_MESSAGE_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'Send a message to your parent node, to one or more of your direct children, or (root '
            'node only) to the front-door agent. Each target is judged on its own: a terminal target '
            'rejects and says so, a paused target still accepts a machine message (that is how it '
            'gets unstuck), and an out-of-scope target is rejected with the addresses you may use. '
            'Use `请求` when you are about to park waiting for an answer — it wakes the recipient '
            'immediately; `进展` and `指令` only wake a recipient whose own wait policy says so. '
            'Large bodies belong in `ref`; the message itself should stay a summary.'
        )

    @property
    def model_description(self) -> str:
        return 'Message your parent, children, or the front door. Use 请求 when you will wait for an answer.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'to': {
                    'type': ['string', 'array'],
                    'items': {'type': 'string'},
                    'description': (
                        "`parent`, `frontdoor` (root only), a direct child node id, or a list of them."
                    ),
                    'minLength': 1,
                },
                'intent_kind': {
                    'type': 'string',
                    'enum': ['进展', '请求', '指令'],
                    'description': (
                        '进展 = report without expecting a reply; 请求 = you will park and wait for an '
                        'answer; 指令 = tell someone below you what to do.'
                    ),
                },
                'content': {'type': 'string', 'minLength': 1, 'description': 'Message body.'},
                'ref': {
                    'type': 'string',
                    'description': 'Optional pointer to a large artifact; keep `content` a summary then.',
                },
                'in_reply_to': {'type': 'string', 'description': 'Optional id of the message answered.'},
            },
            'required': ['to', 'content'],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not str((params or {}).get('content') or '').strip():
            errors.append('content must not be empty')
        return errors

    async def execute(
        self,
        to: Any = None,
        intent_kind: str = '',
        content: str = '',
        ref: str = '',
        in_reply_to: str = '',
        **kwargs: Any,
    ) -> str:
        _ = kwargs
        result = await self._deliver(
            to=to,
            intent_kind=str(intent_kind or '').strip(),
            content=str(content or '').strip(),
            ref=str(ref or '').strip(),
            in_reply_to=str(in_reply_to or '').strip(),
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


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
            'batch — spawn it later, or use requires_acceptance for single-child acceptance. '
            'Optional per-child `seed_tools` seeds those tool contracts into the child’s first '
            'request; the result reports declared names the runtime did not seed.'
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
                        'Whether this child gets a follow-up acceptance node. Set true when the child output is reused upstream '
                        '(aggregation, the parent answer, a required deliverable), writes or rewrites files, carries count/scope '
                        'thresholds, or spans multiple artifacts or directories. The acceptance node is activated only after this child pipeline '
                        'is terminal — this is the correct way to gate acceptance for a single child; do not spawn a sibling acceptance node in the '
                        'same batch for it.'
                    ),
                },
                'acceptance_prompt': {
                    'type': 'string',
                    'description': 'Prompt for the acceptance node. Required only when requires_acceptance=true.',
                },
                'seed_tools': {
                    'type': 'array',
                    'description': (
                        'Optional. Tool names whose contracts should be delivered in this child node\'s first '
                        'request so the first call can execute directly. Runtime filters by the node\'s role and '
                        'reports every dropped name back on this child. Omit when unsure.'
                    ),
                    'items': {
                        'type': 'string',
                    },
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
                'seed_tools': {
                    'type': 'array',
                    'items': {'type': 'string'},
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
