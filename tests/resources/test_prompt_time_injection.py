"""模型时间锚点注入的单元测试。

背景：模型上下文原本没有任何"现在几点"的信息，cron 事件只带裸毫秒时间戳，
实测发生过心算时区出错与编造当前时间两类事故。本组测试锁定注入契约：
- g3ku.core.timefmt 的渲染/剥离 helper
- 用户消息投影装饰（持久化保持原文，投影副本追加送达时间戳）
- cron 内部消息的人类可读时间字段
- heartbeat 事件束的唤醒时刻与 task_terminal 的 finished_at 渲染
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from g3ku.core.timefmt import (
    MESSAGE_ARRIVAL_TIME_MARKER,
    render_arrival_stamp,
    render_epoch_ms_local,
    render_local_time,
    strip_arrival_time_stamp,
)


class TestTimefmt:
    def test_render_local_time_includes_offset_and_weekday(self) -> None:
        moment = datetime(2026, 9, 4, 23, 0, 4, tzinfo=timezone(timedelta(hours=8)))
        text = render_local_time(moment)
        assert text.startswith("2026-09-04 23:00:04 +08:00")
        assert "周五" in text

    def test_render_local_time_naive_assumes_local(self) -> None:
        text = render_local_time(datetime(2026, 9, 4, 23, 0, 4))
        assert "2026-09-04 23:00:04" in text

    def test_render_local_time_default_is_now(self) -> None:
        text = render_local_time()
        assert str(datetime.now().year) in text

    def test_render_epoch_ms_local_matches_incident_timestamp(self) -> None:
        # 9/4 事故时间戳：模型曾把 1788534000000 心算成"9/5 07:00"，
        # 实际是 2026-09-04 23:00:00 GMT+8（本仓库运行机器即 +08:00）。
        text = render_epoch_ms_local(1788534000000)
        local = datetime.fromtimestamp(1788534000).astimezone()
        assert text.startswith(local.strftime("%Y-%m-%d %H:%M:%S"))
        if local.utcoffset() == timedelta(hours=8):
            assert text.startswith("2026-09-04 23:00:00 +08:00")

    def test_render_epoch_ms_local_rejects_invalid(self) -> None:
        assert render_epoch_ms_local(None) == ""
        assert render_epoch_ms_local("abc") == ""
        assert render_epoch_ms_local(0) == ""
        assert render_epoch_ms_local(-5) == ""

    def test_arrival_stamp_round_trip(self) -> None:
        stamp = render_arrival_stamp("2026-09-05T10:38:39.123456")
        assert stamp.startswith("\n\n" + MESSAGE_ARRIVAL_TIME_MARKER)
        assert "2026-09-05 10:38:39" in stamp
        original = "帮我查一下任务进展"
        assert strip_arrival_time_stamp(original + stamp) == original

    def test_arrival_stamp_accepts_datetime(self) -> None:
        stamp = render_arrival_stamp(datetime(2026, 9, 5, 10, 38, 39))
        assert "2026-09-05 10:38:39" in stamp

    def test_arrival_stamp_invalid_returns_empty(self) -> None:
        assert render_arrival_stamp(None) == ""
        assert render_arrival_stamp("") == ""
        assert render_arrival_stamp("not-a-timestamp") == ""

    def test_strip_only_removes_trailing_marker_line(self) -> None:
        # 标记出现在正文中间时不剥离（只有投影追加的末尾行才是装饰）。
        text = f"提到 {MESSAGE_ARRIVAL_TIME_MARKER} 的正文\n后续内容"
        assert strip_arrival_time_stamp(text) == text


class TestUserMessageProjectionDecoration:
    def _builder(self):
        from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder

        return CeoMessageBuilder

    def test_decorates_user_record_with_timestamp(self) -> None:
        record = {
            "role": "user",
            "content": "查一下任务",
            "timestamp": "2026-09-05T10:38:39.123456",
        }
        entry = self._builder()._history_message(record)
        assert entry["content"].startswith("查一下任务\n\n" + MESSAGE_ARRIVAL_TIME_MARKER)
        assert "2026-09-05 10:38:39" in entry["content"]
        # 持久化记录本身不被改动
        assert record["content"] == "查一下任务"

    def test_skips_records_without_timestamp(self) -> None:
        record = {"role": "user", "content": "查一下任务"}
        entry = self._builder()._history_message(record)
        assert entry["content"] == "查一下任务"

    def test_skips_internal_messages(self) -> None:
        record = {
            "role": "user",
            "content": "[SESSION EVENTS] ...",
            "timestamp": "2026-09-05T10:38:39.123456",
            "metadata": {"heartbeat_internal": True},
        }
        entry = self._builder()._history_message(record)
        assert MESSAGE_ARRIVAL_TIME_MARKER not in entry["content"]

    def test_skips_non_user_roles(self) -> None:
        record = {
            "role": "assistant",
            "content": "已完成",
            "timestamp": "2026-09-05T10:38:39.123456",
        }
        entry = self._builder()._history_message(record)
        assert MESSAGE_ARRIVAL_TIME_MARKER not in entry["content"]

    def test_idempotent_for_already_decorated_content(self) -> None:
        record = {
            "role": "user",
            "content": f"查一下任务\n\n{MESSAGE_ARRIVAL_TIME_MARKER} 2026-09-05 10:38:39 +08:00（周六）",
            "timestamp": "2026-09-05T10:38:39.123456",
        }
        entry = self._builder()._history_message(record)
        assert entry["content"].count(MESSAGE_ARRIVAL_TIME_MARKER) == 1

    def test_projection_is_deterministic_for_cache_stability(self) -> None:
        record = {
            "role": "user",
            "content": "查一下任务",
            "timestamp": "2026-09-05T10:38:39.123456",
        }
        first = self._builder()._history_message(record)["content"]
        second = self._builder()._history_message(record)["content"]
        assert first == second


class TestCronMessageTimeFields:
    def test_system_message_carries_delivery_time(self) -> None:
        from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport

        message = CeoFrontDoorSupport._cron_internal_system_message(
            {
                "cron_internal": True,
                "cron_job_id": "job-1",
                "cron_max_runs": 3,
                "cron_delivery_index": 2,
                "cron_delivered_at_ms": 1788534000000,
            }
        )
        content = str(message.get("content") or "")
        assert "本次提醒送达时间：" in content
        assert "不要自行心算毫秒时间戳" in content
        local_tz = datetime.now().astimezone().tzinfo
        local = datetime.fromtimestamp(1788534000, tz=local_tz)
        assert local.strftime("%Y-%m-%d %H:%M:%S") in content

    def test_event_message_carries_local_conversions(self) -> None:
        from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport

        message = CeoFrontDoorSupport._cron_internal_event_message(
            {
                "cron_internal": True,
                "cron_job_id": "job-1",
                "cron_delivery_index": 2,
                "cron_max_runs": 3,
                "cron_delivered_runs": 1,
                "cron_scheduled_run_at_ms": 1788534000000,
                "cron_last_delivered_at_ms": 1788447600000,
                "cron_delivered_at_ms": 1788534004000,
                "cron_reminder_text": "执行日报任务",
            },
            reminder_text="执行日报任务",
        )
        content = str(message.get("content") or "")
        payload = json.loads(content.split("\n", 1)[1])
        # 原始毫秒字段保留（契约兼容）
        assert payload["scheduled_run_at_ms"] == 1788534000000
        assert payload["last_delivered_at_ms"] == 1788447600000
        # 人类可读换算字段（期望值按运行机器本地时区动态计算，避免用例绑定 +08:00）
        local_tz = datetime.now().astimezone().tzinfo

        def _local_prefix(ms: int) -> str:
            return datetime.fromtimestamp(ms / 1000, tz=local_tz).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        assert payload["delivered_at_local"].startswith(_local_prefix(1788534004000))
        assert payload["scheduled_run_at_local"].startswith(_local_prefix(1788534000000))
        assert payload["last_delivered_at_local"].startswith(_local_prefix(1788447600000))

    def test_event_message_omits_local_fields_when_ms_missing(self) -> None:
        from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport

        message = CeoFrontDoorSupport._cron_internal_event_message(
            {
                "cron_internal": True,
                "cron_job_id": "job-1",
                "cron_delivery_index": 1,
                "cron_max_runs": 1,
            },
            reminder_text="提醒",
        )
        payload = json.loads(str(message.get("content") or "").split("\n", 1)[1])
        assert "scheduled_run_at_local" not in payload
        assert "last_delivered_at_local" not in payload
        # delivered_at_local 始终存在（回退 now()）
        assert str(datetime.now().year) in payload["delivered_at_local"]


class TestHeartbeatEventBundleTime:
    def test_bundle_header_carries_wakeup_time(self) -> None:
        from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

        lane = build_heartbeat_prompt_lane(
            provider_model="",
            stable_rules_text="rules",
            events=[
                {
                    "event_reason": "task_terminal",
                    "task_id": "t1",
                    "title": "日报任务",
                    "status": "success",
                    "brief_text": "done",
                    "finished_at": (
                        datetime.now().astimezone() - timedelta(hours=2)
                    ).isoformat(timespec="seconds"),
                }
            ],
        )
        bundle = lane.event_bundle_text
        lines = bundle.split("\n")
        # 标记行保持在前（compaction 与 startswith 检测依赖），时间行紧随其后
        assert lines[0] == "[SESSION EVENTS]"
        assert lines[1] == "## EVENT BUNDLE"
        assert lines[2].startswith("当前时间（本次唤醒时刻）：")
        assert str(datetime.now().year) in lines[2]

    def test_task_terminal_renders_finished_at(self) -> None:
        from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

        finished = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(
            timespec="seconds"
        )
        lane = build_heartbeat_prompt_lane(
            provider_model="",
            stable_rules_text="rules",
            events=[
                {
                    "event_reason": "task_terminal",
                    "task_id": "t1",
                    "title": "日报任务",
                    "status": "success",
                    "brief_text": "done",
                    "finished_at": finished,
                }
            ],
        )
        assert "Finished at:" in lane.event_bundle_text
        # format_local_timestamp 输出 ISO 本地偏移格式
        expected_minute = (datetime.now().astimezone() - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        assert expected_minute in lane.event_bundle_text

    def test_task_terminal_without_finished_at_has_no_time_line(self) -> None:
        from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

        lane = build_heartbeat_prompt_lane(
            provider_model="",
            stable_rules_text="rules",
            events=[
                {
                    "event_reason": "task_terminal",
                    "task_id": "t1",
                    "status": "success",
                    "brief_text": "done",
                }
            ],
        )
        assert "Finished at:" not in lane.event_bundle_text
