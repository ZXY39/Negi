"""前门车道：`time` 与 `schedule_wake` 作为常驻内置的曝光合同，以及会话侧唤醒的落点。

`docs/architecture/tool-and-skill-system.md`「3.1.1 为什么"常驻"需要独立机制」列过四处注入点，
`silent` 每一处都踩过一次"名字看得见却调不动"的实盘。这组用例把同一套判据钉在新添的两颗
工具上：少任何一处，症状都是"模型从不使用它"或"调了就报错"，而两者都不会指向注册环节。

前门侧 `schedule_wake` 的落点是一次性 cron 任务（不是新写一个会话计时器），所以这里同时钉住
"重新设就是覆盖"与"调度器缺席时响亮失败"。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from g3ku.agent.tools.registry import ToolRegistry
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from g3ku.runtime.wake_clock import WAKE_JOB_NAME_PREFIX, WAKE_MESSAGE_HEAD
from main.runtime.internal_tools import ScheduleWakeTool
from main.runtime.tool_names import (
    SCHEDULE_WAKE_TOOL_NAME,
    SILENT_TOOL_NAME,
    SYNTHETIC_RESIDENT_TOOL_NAMES,
    TIME_TOOL_NAME,
)


def _ops(*, cron_service=None) -> CeoFrontDoorRuntimeOps:
    """只带本组断言所需依赖的实例（绕过完整 __init__），与 silent 曝光用例同一手法。"""
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(tools=ToolRegistry(), app_config=None, cron_service=cron_service)
    ops._build_tool_runtime_context = lambda **kwargs: {}
    return ops


def _runtime(session_key: str = 'web:ceo-1') -> SimpleNamespace:
    return SimpleNamespace(
        context=SimpleNamespace(
            session=SimpleNamespace(
                state=SimpleNamespace(session_key=session_key),
                _channel='cli',
                _chat_id=session_key,
            )
        )
    )


class _FakeCron:
    def __init__(self, jobs=None, *, raise_on_add: Exception | None = None):
        self.added: list[dict] = []
        self.removed: list[str] = []
        self.jobs = list(jobs or [])
        self.raise_on_add = raise_on_add

    def add_job(self, **kwargs):
        if self.raise_on_add is not None:
            raise self.raise_on_add
        self.added.append(kwargs)
        return SimpleNamespace(id=f'job{len(self.added)}', name=kwargs.get('name', ''))

    def list_jobs(self, *, include_disabled: bool = False):
        _ = include_disabled
        return list(self.jobs)

    def remove_job(self, job_id: str):
        self.removed.append(job_id)
        return True


def _job(*, job_id: str, name: str, session_key: str, kind: str = 'at'):
    return SimpleNamespace(
        id=job_id,
        name=name,
        schedule=SimpleNamespace(kind=kind),
        payload=SimpleNamespace(session_key=session_key),
    )


def _wake_tool(ops: CeoFrontDoorRuntimeOps):
    """从执行束里取那颗 wake 工具：验的就是线上真正会被调到的那个实例。"""
    state = dict(initial_persistent_state(user_input={'content': 'x', 'metadata': {}}))
    bundle = ops._frontdoor_execution_bundle(state=state, runtime=_runtime('web:ceo-42'))
    tool = bundle.visible_tools[SCHEDULE_WAKE_TOOL_NAME]
    assert isinstance(tool, ScheduleWakeTool)
    return tool


class TestResidentExposure:
    def test_message_builder_injects_both_without_a_visibility_precondition(self):
        names = CeoMessageBuilder._callable_tool_names(
            visible_tool_names=['create_async_task', 'exec'],
            contract_present_tool_names=None,
        )
        assert TIME_TOOL_NAME in names
        assert SCHEDULE_WAKE_TOOL_NAME in names
        # 追加在真实能力之后，不是前插：既有 callable 顺序不变。
        assert names.index(TIME_TOOL_NAME) > names.index('exec')

    def test_both_survive_every_lane_state(self):
        ops = _ops()
        states = [
            {'tool_names': ['exec'], 'provider_tool_names': ['exec']},
            {'tool_names': [], 'provider_tool_names': []},
            {'tool_names': ['exec'], 'heartbeat_internal': True},
            {'tool_names': ['exec'], 'cron_internal': True},
            {},
        ]
        for index, state in enumerate(states):
            callable_names = ops._frontdoor_callable_tool_names_for_state(state)
            visible_names = ops._frontdoor_runtime_visible_tool_names_for_state(state)
            for name in (TIME_TOOL_NAME, SCHEDULE_WAKE_TOOL_NAME):
                assert name in callable_names, f'state #{index} lost {name} from callable'
                assert name in visible_names, f'state #{index} lost {name} from visible'

    def test_execution_bundle_resolves_both_objects(self):
        """④：执行侧查的是 bundle 的对象字典。名字在名单里而对象不在，就是那条
        `Error: tool not available` 的旧失败形状。"""
        ops = _ops()
        state = dict(initial_persistent_state(user_input={'content': 'x', 'metadata': {}}))
        bundle = ops._frontdoor_execution_bundle(state=state, runtime=_runtime())
        for name in SYNTHETIC_RESIDENT_TOOL_NAMES:
            assert name in bundle.visible_tools, f'{name} missing from visible_tools'
            assert name in bundle.declared_tools, f'{name} missing from declared_tools'
            assert bundle.visible_tools[name].name == name
        assert bundle.visible_tools[SCHEDULE_WAKE_TOOL_NAME].parameters['required'] == ['note']

    def test_synthetic_names_are_not_resolved_from_the_registry(self):
        """准入名单里剔掉合成实例：它们不在 registry，留着只会让按名取实例空转。"""
        ops = _ops()
        state = {
            'tool_names': ['exec', TIME_TOOL_NAME, SCHEDULE_WAKE_TOOL_NAME, SILENT_TOOL_NAME],
            'rbac_visible_tool_names': ['exec'],
            'provider_tool_names': ['exec'],
        }
        executable = ops._frontdoor_executable_tool_names(state)
        assert 'exec' in executable
        for name in SYNTHETIC_RESIDENT_TOOL_NAMES:
            assert name not in executable, f'{name} must not be resolved from the registry'


class TestSessionWakeBackend:
    def test_arms_one_shot_job_bound_to_this_session(self):
        cron = _FakeCron()
        ops = _ops(cron_service=cron)
        raw = asyncio.run(
            ops._frontdoor_schedule_wake(
                state={},
                runtime=_runtime('web:ceo-42'),
                wake_at_ms=1_792_983_600_000,
                note='广播下一幕',
            )
        )
        assert raw['scope'] == 'session'
        assert len(cron.added) == 1
        job = cron.added[0]
        assert job['schedule'].kind == 'at' and job['schedule'].at_ms == 1_792_983_600_000
        assert job['session_key'] == 'web:ceo-42'
        assert job['delete_after_run'] is True and job['max_runs'] == 1
        # 正文自带"这是你自己设的"：cron 车道不给内部回合加任何标签。
        assert job['message'].startswith(WAKE_MESSAGE_HEAD)
        assert '广播下一幕' in job['message']
        assert job['name'].startswith(WAKE_JOB_NAME_PREFIX)

    def test_the_tool_adds_the_arming_verdict_on_top_of_the_backend(self):
        """`armed` 由工具盖章，后端只回读数/错误：两条车道共用同一份判定，前门不必再写一遍。"""
        cron = _FakeCron()
        ops = _ops(cron_service=cron)
        tool = _wake_tool(ops)
        parsed = json.loads(asyncio.run(tool.execute(note='广播下一幕', after_seconds=120)))
        assert parsed['armed'] is True
        assert parsed['scope'] == 'session'
        assert parsed['wake_id'] == 'job1'

    def test_rearming_replaces_the_previous_self_wake_only(self):
        """覆盖只针对自己那一族：同一时刻的用户提醒、别的会话的闹钟都不在射程里。"""
        cron = _FakeCron(
            jobs=[
                _job(job_id='mine', name=f'{WAKE_JOB_NAME_PREFIX}旧闹钟', session_key='web:ceo-42'),
                _job(job_id='user-reminder', name='给用户开会提醒', session_key='web:ceo-42'),
                _job(job_id='other-session', name=f'{WAKE_JOB_NAME_PREFIX}别人的', session_key='web:ceo-99'),
                _job(job_id='recurring', name=f'{WAKE_JOB_NAME_PREFIX}周期', session_key='web:ceo-42', kind='every'),
            ]
        )
        ops = _ops(cron_service=cron)
        asyncio.run(
            ops._frontdoor_schedule_wake(
                state={}, runtime=_runtime('web:ceo-42'), wake_at_ms=1_792_983_600_000, note='x'
            )
        )
        assert cron.removed == ['mine']
        assert len(cron.added) == 1

    def test_missing_scheduler_fails_loudly_instead_of_claiming_it_is_armed(self):
        """CLI 起的会话、或 cron 关掉时：回执必须自己说没设上。"""
        ops = _ops(cron_service=None)
        raw = asyncio.run(
            ops._frontdoor_schedule_wake(state={}, runtime=_runtime(), wake_at_ms=1_792_983_600_000, note='x')
        )
        assert raw == {'error': 'wake_scheduler_unavailable'}
        assert 'armed' not in raw or raw.get('armed') is not True

    def test_rejected_instant_is_returned_to_the_model(self):
        """cron 自己拒的那句（同一瞬间已有闹钟、时刻已过期）原样回给模型，并由工具盖上 armed=false。"""
        cron = _FakeCron(raise_on_add=ValueError('任务定时已过期，当前时间为 2026-10-26 10:00:00 +08:00'))
        ops = _ops(cron_service=cron)
        raw = asyncio.run(
            ops._frontdoor_schedule_wake(state={}, runtime=_runtime(), wake_at_ms=1_000, note='x')
        )
        assert '已过期' in raw['error']
        parsed = json.loads(asyncio.run(_wake_tool(ops).execute(note='x', after_seconds=120)))
        assert parsed['armed'] is False
        assert '已过期' in parsed['error']
