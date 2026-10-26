"""`time` 与 `schedule_wake`：两颗常驻内置的模型可见合同与时刻解析。

这组断言存在的理由都是实盘形状，不是假想：

- 报时节点此前只能用 `exec Start-Sleep` 当时钟（task:4555863a6a49），所以
  `schedule_wake` 的描述必须明写"它不代替停靠、也不代替睡眠"。
- 角色节点在记录里写"09:52"这类估的时刻，父节点不得不发一条"现在是 03:14"去纠正，
  所以 `time` 必须一次就把本地 ISO、UTC、epoch 毫秒都给出来，别让模型去换算。
- 两条车道共用同一份描述，所以描述里不许出现只对一条车道成立的词。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from g3ku.runtime.tool_visibility import NON_HYDRATABLE_TOOL_NAMES
from g3ku.runtime.wake_clock import (
    WAKE_HORIZON_SECONDS,
    clock_payload,
    iso_from_ms,
    resolve_wake_moment,
)
from main.runtime.internal_tools import ScheduleWakeTool, TimeTool
from main.runtime.tool_names import (
    PROGRESS_NEUTRAL_TOOL_NAMES,
    SCHEDULE_WAKE_TOOL_NAME,
    SYNTHETIC_RESIDENT_TOOL_NAMES,
    TIME_TOOL_NAME,
)


def _now_ms() -> int:
    return int(datetime.now().astimezone().timestamp() * 1000)


def _far_future_ms() -> int:
    return _now_ms() + (WAKE_HORIZON_SECONDS + 3_600) * 1000


def _arm_capture() -> tuple[list[dict], ScheduleWakeTool]:
    calls: list[dict] = []

    async def arm(*, wake_at_ms: int, note: str) -> dict:
        calls.append({'wake_at_ms': wake_at_ms, 'note': note})
        return {'wake_at': iso_from_ms(wake_at_ms), 'epoch_ms': wake_at_ms}

    return calls, ScheduleWakeTool(arm)


class TestSurface:
    def test_names_are_the_two_resident_additions(self):
        assert TimeTool().name == TIME_TOOL_NAME == 'time'
        assert _arm_capture()[1].name == SCHEDULE_WAKE_TOOL_NAME == 'schedule_wake'

    def test_both_are_instant_internal_protocol_tools(self):
        """统一 timeout 参数对它们没意义，但重放必须是安全的（恢复道读这个位）。"""
        for tool in (TimeTool(), _arm_capture()[1]):
            assert tool.hide_universal_timeout_parameter is True
            assert tool.rerun_safe is True

    def test_neither_offers_reuse_args_nor_carries_a_contract(self):
        """两颗都没有 toolskill：契约在场判据与入参暂存都必须绕过它们。"""
        assert {TIME_TOOL_NAME, SCHEDULE_WAKE_TOOL_NAME} <= NON_HYDRATABLE_TOOL_NAMES
        assert {TIME_TOOL_NAME, SCHEDULE_WAKE_TOOL_NAME} <= set(SYNTHETIC_RESIDENT_TOOL_NAMES)

    def test_time_is_the_only_progress_neutral_member(self):
        """`schedule_wake` 逐字相同的重复设闹钟确实是空转，该让守卫看着。"""
        assert PROGRESS_NEUTRAL_TOOL_NAMES == frozenset({TIME_TOOL_NAME})

    def test_descriptions_are_lane_neutral(self):
        """两条车道共用同一颗工具 ⇒ 模型可见文本里不许出现只对一条车道成立的称呼。

        两处都查：`model_description` 是 `to_model_schema()` 真正送上线的那一份，
        `description` 也留在 runtime 内部与 Tool Admin 里，分叉了就会被当成两条合同。
        """
        for tool in (TimeTool(), _arm_capture()[1]):
            text = f'{tool.model_description} {tool.description}'.lower()
            for lane_word in (' node', 'session', 'task_id', 'cron', 'mailbox', 'enter_wait'):
                assert lane_word not in text, f'{tool.name} text is lane-specific: {lane_word}'

    def test_model_visible_text_carries_the_shape_the_provider_strips_away(self):
        """参数级 description 在 provider 边界被整层剥掉，所以形状只能住在函数级文本里。

        缺任何一条，模型都在猜：猜 `at`/`after_seconds` 能不能同时给、猜旧闹钟会不会留在
        场上、猜设完闹钟这一跳会不会自己停。
        """
        text = _arm_capture()[1].model_description
        assert 'exactly one of `after_seconds` or a future `at`' in text
        assert 'replaces your previous pending wake-up' in text
        assert 'does not end this turn' in text
        assert 'time tool' in text  # 绝对时刻从时钟取，不要模型自己换算
        assert 'restart' in text  # 与 `exec Start-Sleep` 的分界就是这条

    def test_time_names_every_reading_it_returns(self):
        text = TimeTool().model_description
        for key in ('`now`', '`utc`', '`epoch_ms`', '`weekday`'):
            assert key in text

    def test_time_takes_no_arguments(self):
        assert TimeTool().parameters['properties'] == {}


class TestTimePayload:
    def test_epoch_and_iso_agree(self):
        """这条钉的是"心算毫秒"那个具体失败形状：给出的几个写法必须互相对得上。

        ISO 按秒取整（`timespec='seconds'`），所以只要求两份写法落在同一秒内；
        真正的性质是"模型抄哪个都不会偏几个小时"，不是逐毫秒相等。
        """
        payload = json.loads(asyncio.run(TimeTool().execute()))
        assert abs(payload['epoch_ms'] - int(datetime.fromisoformat(payload['now']).timestamp() * 1000)) <= 1000
        assert abs(payload['epoch_ms'] - int(datetime.fromisoformat(payload['utc']).timestamp() * 1000)) <= 1000
        assert payload['utc'].endswith('+00:00')
        assert payload['weekday'] in {
            'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'
        }
        assert payload['utc_offset'].startswith(('+', '-'))

    def test_payload_shape_is_stable(self):
        """键集合恒定：描述里承诺的四种写法少一个，模型就得回去读 schema。"""
        assert set(json.loads(asyncio.run(TimeTool().execute()))) == set(clock_payload())


class TestWakeArgumentLanes:
    @pytest.mark.parametrize(
        'payload, reason',
        [
            ({'note': 'x'}, 'neither'),
            ({'note': 'x', 'at': '2099-01-01T00:00:00', 'after_seconds': 60}, 'both'),
            ({'note': 'x', 'at': '2020-01-01T00:00:00'}, 'past'),
            ({'note': 'x', 'at': 'noon tomorrow'}, 'unreadable'),
            ({'note': 'x', 'after_seconds': 0}, 'zero delay'),
            ({'note': 'x', 'after_seconds': -5}, 'negative delay'),
            ({'note': 'x', 'at': iso_from_ms(_far_future_ms())}, 'beyond horizon'),
        ],
    )
    def test_refusals_carry_the_current_time(self, payload, reason):
        """每条打回都要自带"现在几点"，否则模型只能再调一次时钟才知道怎么改。"""
        _calls, tool = _arm_capture()
        parsed = json.loads(asyncio.run(tool.execute(**payload)))
        assert parsed['armed'] is False, reason
        assert parsed['error'].strip()
        assert 'Current time:' in parsed['error'], reason

    def test_empty_note_is_refused_before_any_scheduling(self):
        """`note` 是到点时唯一能读到的东西；空 note 的闹钟等于没设。"""
        calls, tool = _arm_capture()
        parsed = json.loads(asyncio.run(tool.execute(note='   ', after_seconds=60)))
        assert parsed['armed'] is False
        assert calls == []

    def test_relative_and_absolute_forms_reach_the_backend_as_one_shape(self):
        calls, tool = _arm_capture()
        asyncio.run(tool.execute(note='播报下一幕', after_seconds=120))
        absolute = iso_from_ms(_now_ms() + 120_000)
        asyncio.run(tool.execute(note='播报下一幕', at=absolute))
        assert [call['note'] for call in calls] == ['播报下一幕', '播报下一幕']
        assert all(call['wake_at_ms'] > _now_ms() for call in calls)

    def test_naive_iso_is_read_in_the_runtime_local_offset(self):
        """模型抄回去的就是 `time` 那份带偏移的串；给一份 naive 的也必须按同一偏移读，
        否则"10:33 唤醒"会偏好几个小时。"""
        _calls, tool = _arm_capture()
        target = _now_ms() + 3_600_000
        naive = datetime.fromtimestamp(target / 1000).astimezone().replace(tzinfo=None).isoformat(
            timespec='seconds'
        )
        parsed = json.loads(asyncio.run(tool.execute(note='x', at=naive)))
        assert parsed['armed'] is True
        assert abs(int(parsed['epoch_ms']) - target) < 1000

    def test_epoch_millis_from_the_clock_round_trips(self):
        _calls, tool = _arm_capture()
        target = _now_ms() + 600_000
        parsed = json.loads(asyncio.run(tool.execute(note='x', at=str(target))))
        assert parsed['armed'] is True
        assert int(parsed['epoch_ms']) == target

    def test_too_long_note_is_trimmed_and_says_so(self):
        """截断不许静默：模型以为原文进了闹钟，到点却少了一截。"""
        _calls, tool = _arm_capture()
        parsed = json.loads(asyncio.run(tool.execute(note='y' * (tool.NOTE_LIMIT + 50), after_seconds=60)))
        assert parsed['armed'] is True
        assert parsed['note_truncated'] is True


class TestResolveWakeMoment:
    def test_horizon_is_the_same_number_the_error_text_quotes(self):
        _wake, refusal = resolve_wake_moment(
            at=iso_from_ms(_now_ms() + (WAKE_HORIZON_SECONDS + 10) * 1000),
        )
        assert f'{WAKE_HORIZON_SECONDS // 86400} days' in refusal

    def test_max_boundary_inside_the_horizon_is_accepted(self):
        moment, refusal = resolve_wake_moment(after_seconds=WAKE_HORIZON_SECONDS - 60)
        assert refusal == ''
        assert moment > 0

