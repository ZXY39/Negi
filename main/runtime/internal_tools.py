from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.runtime.wake_clock import clock_payload, resolve_wake_moment
from main.models import (
    NodeEvidenceItem,
    SpawnChildResult,
    SpawnChildSpec,
    build_execution_policy_schema,
)
from main.runtime.tool_names import (
    ENTER_WAIT_TOOL_NAME,
    FINAL_RESULT_TOOL_NAME,
    SCHEDULE_WAKE_TOOL_NAME,
    SEND_MESSAGE_TOOL_NAME,
    SILENT_TOOL_NAME,
    TIME_TOOL_NAME,
    VIEW_MESSAGE_HISTORY_TOOL_NAME,
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
    """停靠直到收到一条消息。

    判据只有一条：信箱里有符合条件的未读时**不睡**，立即把它们注入本回合；
    否则本回合结束、节点进入 `waiting_explicit`，任意一条后续消息都会唤醒它。
    子节点的 `child_final` 是否攒齐由 `spawn_child_nodes.wait_all_children`
    决定，停靠本身不再选策略。
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
            'Stop this turn and wait until a message wakes you. If qualifying mail is already '
            'waiting, this does not park you — it injects that mail and the turn continues. '
            '`note` is echoed back to you once on wake so you remember what you were waiting for. '
            'Do not call this while you still have work you can do now.'
        )

    @property
    def model_description(self) -> str:
        return 'Park until a message wakes you. `note` is echoed once on wake.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'note': {
                    'type': 'string',
                    'description': 'What you are waiting for. Echoed back once when you wake.',
                },
            },
            'required': [],
        }

    async def execute(self, note: str = '', **kwargs: Any) -> str:
        _ = kwargs
        result = await self._park(note=str(note or '').strip())
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


class TimeTool(Tool):
    """当前时刻的权威读数。

    两条车道共用同一个实例，无任何依赖注入：节点与前门都只是"问一次表"。它存在的理由是
    上下文里没有时钟——消息不带时刻时，模型只能按消息顺序和记忆里的工作流猜"现在几点"，
    实盘就出现过角色把场景时间写成真实时刻、以及心算 epoch 毫秒算错归属日期。
    """

    hide_universal_timeout_parameter = True
    # 纯读，重放不产生任何副作用。
    rerun_safe = True

    @property
    def name(self) -> str:
        return TIME_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'Read the wall clock. Returns `now` (local time with its UTC offset), `utc`, `epoch_ms`, '
            'and `weekday`. Nothing in your context tells you what time it is: message order is not a '
            'clock, and converting an epoch value in your head is unreliable. Ask before you reason '
            'about real time — judging a deadline, stamping a record, or setting a wake-up. One read '
            'per decision is enough; the clock does not move inside a turn.'
        )

    @property
    def model_description(self) -> str:
        return (
            'Read the wall clock: `now` (local time with UTC offset), `utc`, `epoch_ms`, `weekday`. '
            'Your context carries no clock — ask it instead of converting timestamps by hand.'
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {}, 'required': []}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return json.dumps(clock_payload(), ensure_ascii=False, sort_keys=True)


class ScheduleWakeTool(Tool):
    """给自己设一次唤醒：到点后收到一条带着 `note` 的消息。

    它存在的理由是 `enter_wait` 只能被消息叫醒，而场上唯一"按真实时钟推进"的角色（报时节点
    那一类）此前只能用 `exec Start-Sleep` 当时钟：那既占一个执行槽，又会被任何重启打断，而
    恢复后模型重新发一次**完整时长**的睡眠，下一次到点就被一路往后推。唤醒时刻必须落在耐久
    载体上（节点落 timer 表、会话落一次性任务），而不是活在某个协程里。

    `rerun_safe`：两条车道的写点都是"每个调用方至多一份待触发唤醒"的 upsert/replace，重放
    只会换掉同一份，不会排进第二次。
    """

    hide_universal_timeout_parameter = True
    rerun_safe = True
    NOTE_LIMIT = 500

    def __init__(self, arm: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self._arm = arm

    @property
    def name(self) -> str:
        return SCHEDULE_WAKE_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'Arm one wake-up for yourself: `note` (what to do when it fires) plus exactly one of '
            '`after_seconds` or `at`. Take `at` from the time tool instead of computing it, and it '
            'must still be in the future. You keep at most one pending wake-up — arming again '
            'replaces it, and nothing repeats unless you arm it again. Arming does not end this turn '
            'and does not make you wait: stop working when you have nothing left to do until it '
            'fires. The deadline is durable, so it still fires when this process restarts first — '
            'that is what it replaces sleeping inside a long-running command, whose deadline is lost.'
        )

    @property
    def model_description(self) -> str:
        return (
            'Arm one wake-up for yourself: `note` plus exactly one of `after_seconds` or a future `at` '
            '(copy it from the time tool). Arming replaces your previous pending wake-up and does not '
            'end this turn; when it fires you get a message carrying your note and the current time. '
            'Use it instead of sleeping inside a long-running command — this deadline survives a '
            'restart, that one does not.'
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'note': {
                    'type': 'string',
                    'minLength': 1,
                    'maxLength': self.NOTE_LIMIT,
                    'description': 'What to do when it fires. Delivered to you verbatim.',
                },
                'after_seconds': {
                    'type': 'integer',
                    'minimum': 1,
                    'description': 'Delay from now, in seconds.',
                },
                'at': {
                    'type': 'string',
                    'description': 'Absolute future time, written the way the time tool prints it.',
                },
            },
            'required': ['note'],
        }

    async def execute(
        self,
        note: str = '',
        at: Any = None,
        after_seconds: Any = None,
        **kwargs: Any,
    ) -> str:
        _ = kwargs
        body = str(note or '').strip()
        truncated = len(body) > self.NOTE_LIMIT
        if truncated:
            body = body[: self.NOTE_LIMIT].rstrip()
        if not body:
            return json.dumps(
                {
                    'armed': False,
                    'error': 'note is required — it is the only thing you will have when the wake-up fires.',
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        wake_at_ms, refusal = resolve_wake_moment(at=at, after_seconds=after_seconds)
        if refusal:
            return json.dumps({'armed': False, 'error': refusal}, ensure_ascii=False, sort_keys=True)
        payload = dict(await self._arm(wake_at_ms=int(wake_at_ms), note=body) or {})
        payload.setdefault('armed', str(payload.get('error') or '').strip() == '')
        if truncated:
            payload['note_truncated'] = True
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class SendMessageTool(Tool):
    """给父节点、子节点/后代、同级节点、或（仅根节点）前门 agent 发一条消息。

    一次可发给多个目标；投递逐目标独立判定。发送本身不会让发送方停靠；需要等
    回应时另行调用 `enter_wait`。工具不做寻址判定：合法目标集合与关系由运行时的
    投递入口算，越址时回执里会带上可用的地址。
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
            'Send a message to your parent node, to execution nodes below you, to a sibling, to a '
            'descendant acceptance node, or (root node only) to the front-door agent. Each target is '
            'judged on its own: a terminal target rejects and says so, a paused target still accepts '
            'a machine message (that is how it gets unstuck), and an out-of-scope target is rejected '
            'with the addresses you may use. Sending does not make you wait; call `enter_wait` when '
            'you need a reply. Large bodies belong in `ref`; the message itself should stay a summary.'
        )

    @property
    def model_description(self) -> str:
        return 'Message your parent, execution descendants, a sibling, a descendant verifier, or the front door (root only).'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'to': {
                    'type': ['string', 'array'],
                    'items': {'type': 'string'},
                    'description': (
                        "`parent`, `frontdoor` (root only), a node id you may message, or a list of them."
                    ),
                    'minLength': 1,
                },
                'content': {'type': 'string', 'minLength': 1, 'description': 'Message body.'},
                'ref': {
                    'type': 'string',
                    'description': 'Optional pointer to a large artifact; keep `content` a summary then.',
                },
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
        content: str = '',
        ref: str = '',
        **kwargs: Any,
    ) -> str:
        _ = kwargs
        result = await self._deliver(
            to=to,
            content=str(content or '').strip(),
            ref=str(ref or '').strip(),
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


class ViewMessageHistoryTool(Tool):
    """本节点收到的全部消息台账（只读，返回可 content_open 的引用）。"""

    hide_universal_timeout_parameter = True

    def __init__(self, history: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self._history = history

    @property
    def name(self) -> str:
        return VIEW_MESSAGE_HISTORY_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'Read every message this node has received since it was created, as a JSON artifact '
            'reference you can open with content_open/content_search. Each record carries the '
            'receive time, sender relationship and node id, message id, content and status. '
            'Messages held by a wait_all_children batch appear as status=held without content '
            'until the batch settles.'
        )

    @property
    def model_description(self) -> str:
        return 'Message history for this node; returns an artifact ref.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {}, 'required': []}

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        result = await self._history()
        return json.dumps(result, ensure_ascii=False, sort_keys=True)


class SpawnChildNodesTool(Tool):
    hide_universal_timeout_parameter = True
    # 拆掉阻塞承诺后本工具只做"创建 + 交棒"，毫秒级返回，不再需要豁免通用超时：
    # 子节点的运行时长与父节点的工具调用无关。
    # 重跑按 tool_call_id 命中父节点 metadata 里的 spawn_operations 记录：同一 call id
    # 只返回同一批创建回执，不会再物化一批子节点。
    rerun_safe = True

    def __init__(
        self,
        spawn_callback: Callable[
            [list[SpawnChildSpec], str | None, bool], Awaitable[list[SpawnChildResult]]
        ],
    ) -> None:
        self._spawn_callback = spawn_callback

    @property
    def name(self) -> str:
        return 'spawn_child_nodes'

    @property
    def description(self) -> str:
        return (
            'Create child nodes and hand them to the scheduler. The call returns as soon as they '
            'exist — it does NOT wait for them, and it does not contain their results. Each child '
            'reports its terminal result to you as a child_final message; call enter_wait to be '
            'woken when a message arrives. `wait_all_children=true` holds this batch\'s child_final '
            'messages until every created execution child in the batch is terminal, then delivers '
            'them together; default false delivers each result as it lands. Only mutually independent '
            'branches belong in one batch; a node that consumes its siblings\' outputs must be '
            'created in a later call, after you have received those siblings\' results. Use '
            'requires_acceptance for per-child acceptance; the acceptance node is created with the '
            'child and runs after the child submits.'
        )

    @property
    def model_description(self) -> str:
        return (
            'Create child nodes for delegated work and return immediately with their ids. One call = '
            'one batch (no ordering); results arrive later as child_final messages — call enter_wait '
            'to wait for mail. Set `wait_all_children` to deliver the batch\'s results only after all '
            'its children finish. Never put a node that consumes its siblings’ outputs in the same '
            'batch; create it after you have their results. Optional per-child `seed_tools` seeds '
            'those tool contracts into the child’s first request.'
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
                        'One batch of children created together (no ordering guarantee). The call returns as soon as '
                        'they exist; each child reports its result later as a child_final message. Put only mutually '
                        'independent, ready branches in one batch. A child that depends on other children’s outputs '
                        '(e.g. an acceptance/verification/aggregation node for its siblings) must be created in a separate '
                        'later call, after you have received those results.'
                    ),
                    'items': child_schema,
                    'minItems': 1,
                },
                'wait_all_children': {
                    'type': 'boolean',
                    'description': (
                        'When true, hold this batch\'s child_final messages until every created execution '
                        'child in the batch is terminal, then deliver them together. Default false: each '
                        'child result is delivered as it arrives.'
                    ),
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
                'wait_all_children': {'type': 'boolean'},
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

    async def execute(
        self,
        children: list[dict[str, Any]],
        wait_all_children: bool = False,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        if not runtime:
            fallback = kwargs.get('__g3ku_runtime')
            runtime = fallback if isinstance(fallback, dict) else {}
        specs = [SpawnChildSpec.model_validate(item) for item in list(children or [])]
        batch_id = str(runtime.get('current_tool_call_id') or '')
        results = await self._spawn_callback(
            specs,
            batch_id or None,
            bool(wait_all_children),
        )
        created: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for index, item in enumerate(results):
            goal = str(item.goal or (specs[index].goal if index < len(specs) else '') or '').strip()
            if str(item.state or '').strip() == 'rejected' or bool(item.review_blocked):
                rejected.append(
                    {
                        'goal': goal,
                        'gate': str(item.gate or ('review' if item.review_blocked else 'unknown')).strip(),
                        'reason': str(item.reject_reason or item.node_output or '').strip(),
                    }
                )
                continue
            created.append(
                {
                    'node_id': str(item.child_node_id or '').strip(),
                    'goal': goal,
                    'acceptance_node_id': str(item.acceptance_node_id or '').strip(),
                }
            )
        if created and wait_all_children:
            summary = (
                f'已创建 {len(created)} 个子节点；本批结果会在全部子节点结束后一起以 child_final 送达，'
                '需要停靠时调用 enter_wait，不要轮询。'
            )
        elif created:
            summary = (
                f'已创建 {len(created)} 个子节点；结果会随每个子节点结束逐条以 child_final 送达，'
                '需要停靠时调用 enter_wait，不要轮询。'
            )
        else:
            summary = '没有创建任何子节点。'
        if rejected:
            summary += f' 另有 {len(rejected)} 个候选被拒绝（见 rejected）。'
        return json.dumps(
            {
                'created': created,
                'rejected': rejected,
                'batch_id': batch_id,
                'wait_all_children': bool(wait_all_children),
                'summary': summary,
            },
            ensure_ascii=False,
        )


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
                'Submit this turn\'s acceptance verdict. Make it the only tool call in the turn. '
                'verdict=pass ends the inspected node as successful; verdict=reject sends the feedback back '
                'and the inspected node retries; verdict=reject_no_progress additionally pauses the inspected '
                'node as abnormal, so use it only when the new submission shows no real change against the '
                'previous rejection.'
            )
        return (
            'Submit the final structured result for the current execution node. '
            'Use this only when you are ready to end the node, and make it the only tool call in the turn. '
            'If the node has an acceptance reviewer, the submission enters acceptance instead of ending the node.'
        )

    @property
    def model_description(self) -> str:
        if self._node_kind == 'acceptance':
            return (
                'Submit the acceptance verdict: pass | reject | reject_no_progress, plus feedback. '
                'feedback must explain what is still wrong for reject-like verdicts. The tool returns '
                '{outcome, submission_id, acceptance_node_id}.'
            )
        return (
            'Submit the current result: summary, answer, evidence, remaining_work. '
            'The tool returns {outcome, submission_id, acceptance_node_id}.'
        )

    @property
    def parameters(self) -> dict[str, Any]:
        if self._node_kind == 'acceptance':
            return {
                'type': 'object',
                'properties': {
                    'verdict': {
                        'type': 'string',
                        'enum': ['pass', 'reject', 'reject_no_progress'],
                        'description': (
                            'pass: the submission meets the acceptance criteria. '
                            'reject: repairable rejection with concrete feedback. '
                            'reject_no_progress: the submission repeats a previously rejected flaw without '
                            'meaningful change; the inspected node is paused for an operator.'
                        ),
                    },
                    'feedback': {
                        'type': 'string',
                        'description': 'What is still wrong and what must change. Required for reject verdicts.',
                    },
                },
                'required': ['verdict', 'feedback'],
            }
        return {
            'type': 'object',
            'properties': {
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
                    'description': 'Remaining work items, if any. Informational only.',
                    'items': {'type': 'string'},
                },
            },
            'required': ['summary', 'answer'],
        }

    @property
    def model_parameters(self) -> dict[str, Any]:
        parameters = self.parameters
        for prop in parameters.get('properties', {}).values():
            prop.pop('description', None)
        return parameters

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        if self._node_kind == 'acceptance':
            payload = {
                'verdict': str(kwargs.get('verdict') or '').strip().lower(),
                'feedback': str(kwargs.get('feedback') or '').strip(),
            }
        else:
            payload = {
                'summary': str(kwargs.get('summary') or '').strip(),
                'answer': str(kwargs.get('answer') or ''),
                'evidence': [
                    NodeEvidenceItem.model_validate(item).model_dump(mode='json')
                    for item in list(kwargs.get('evidence') or [])
                    if isinstance(item, dict)
                ],
                'remaining_work': [str(item or '').strip() for item in list(kwargs.get('remaining_work') or [])],
            }
        return await self._submit_callback(payload)
