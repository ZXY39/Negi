"""设计内待命的唤醒重锚：长待命之后的设计内唤醒不得立即误发停滞提醒。

实盘 task:c0430f2b6027（2026-11-07）：连续待命节点 15:41 答复完停机，等它自己设的
巡检闹钟（16:02 到点）。闹钟投递唤醒的那一拍，`run_task → start_task` 用待命前的
旧锚点重新装填定时器——旧锚点 + 20 分钟首档早已过期（delay=0），而唤醒通知此刻恰好
未读，designed_idle 的"无可注入未读"判据被这次唤醒通知自己击败，判定落到
`suspected_stall`，在节点开始干活前一秒发出一条 21 分钟陈旧的停滞提醒。

修复合同：`_schedule` 因 designed_idle 熄表时落 `stall_standby_disarmed_at` 标记；
下一次唤醒的 `start_task` 消费标记，把静默锚点重算到唤醒时刻再装填。非待命路径
（真失速恢复、暂停恢复、bootstrap）没有标记，行为不变；唤醒派发若真丢失
（节点带着未读停在停靠位），20 分钟后仍按 `suspected_stall` 报警——重锚不遮蔽"该醒没醒"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.service.runtime_service import MainRuntimeService
from main.service.task_stall_notifier import (
    STALL_STANDBY_DISARMED_META_KEY,
    TaskStallNotifier,
)


class _ReanchorFakeLogService:
    """state 与 meta 共享同一份字典：镜像真实系统里 read_runtime_state 与 meta 的同源读法。"""

    def __init__(self, state: dict) -> None:
        self._state = state

    def read_runtime_state(self, task_id: str, *, frame_mode: str = 'full'):
        return dict(self._state)

    def read_task_runtime_meta(self, task_id: str):
        return dict(self._state)

    def update_task_runtime_meta(self, task_id: str, **payload):
        self._state.update(payload)
        return dict(self._state)


class _ReanchorFakeService:
    def __init__(self, *, designed_idle: bool = False, actionable: bool = True) -> None:
        self._designed_idle = designed_idle
        self._actionable = actionable
        self._clock = datetime.now(timezone.utc)
        self.state: dict = {
            "last_visible_output_at": "",
            "last_stall_notice_bucket_minutes": 0,
            "stall_standby_disarmed_at": "",
            "frames": [],
        }
        self.log_service = _ReanchorFakeLogService(self.state)
        self.emitted: list[dict] = []
        self.task = SimpleNamespace(
            task_id="task:standby",
            status="in_progress",
            is_paused=False,
            pause_requested=False,
            cancel_requested=False,
            created_at="2026-11-07T00:00:00+00:00",
        )

    def get_task(self, task_id: str):
        return self.task

    def _task_origin_session_id(self, task) -> str:
        return "web:standby"

    def is_task_stall_designed_idle(self, task_id: str, runtime_state=None) -> bool:
        return self._designed_idle

    def is_task_stall_actionable(self, task_id: str, runtime_state=None) -> bool:
        return self._actionable

    def build_task_stall_payload(self, task_id: str, *, bucket_minutes, last_visible_output_at=None):
        return {
            "task_id": task_id,
            "bucket_minutes": bucket_minutes,
            "last_visible_output_at": last_visible_output_at,
        }

    def emit_task_stall(self, payload) -> bool:
        self.emitted.append(dict(payload or {}))
        return True

    def _stall_now_iso(self) -> str:
        return self._clock.isoformat(timespec="microseconds")


def _mark_standby(state: dict, *, minutes_ago: int) -> datetime:
    anchor = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    state["last_visible_output_at"] = anchor.isoformat()
    state[STALL_STANDBY_DISARMED_META_KEY] = anchor.isoformat()
    return anchor


@pytest.mark.asyncio
async def test_designed_idle_disarm_sets_flag_without_arming_a_timer() -> None:
    service = _ReanchorFakeService(designed_idle=True)
    notifier = TaskStallNotifier(service=service)
    try:
        service.state["last_visible_output_at"] = service._stall_now_iso()
        notifier._schedule("task:standby")
        assert notifier._tasks == {}, "designed idle must not arm a stall timer"
        assert str(service.state.get(STALL_STANDBY_DISARMED_META_KEY) or "").strip()
    finally:
        await notifier.close()


@pytest.mark.asyncio
async def test_wake_after_long_standby_reanchors_and_does_not_emit_immediately() -> None:
    """事故回归：待命 25 分钟后闹钟唤醒——重锚后首桶在 20 分钟之后，不在唤醒那一拍。"""
    service = _ReanchorFakeService(designed_idle=False)
    _mark_standby(service.state, minutes_ago=25)
    notifier = TaskStallNotifier(service=service)
    try:
        wake_at = datetime.now(timezone.utc)
        service._clock = wake_at
        notifier.start_task("task:standby")

        meta = service.log_service.read_task_runtime_meta("task:standby")
        # 锚点重算到唤醒这一拍，而不是沿用 25 分钟前的待命前锚点。
        assert meta["last_visible_output_at"] == wake_at.isoformat(timespec="microseconds")
        assert str(meta.get(STALL_STANDBY_DISARMED_META_KEY) or "") == ""
        assert int(meta.get("last_stall_notice_bucket_minutes") or 0) == 0
        # 旧代码在这一拍已经发射（delay=0 → _emit_if_still_due 立即判桶）。
        assert service.emitted == []
        assert "task:standby" in notifier._tasks, "wake must re-arm the stall timer"

        # 即便发射检查在唤醒同一拍跑一次（复刻旧 delay=0 的形状），重锚后的窗口桶值为 0。
        await notifier._emit_if_still_due("task:standby")
        assert service.emitted == []
    finally:
        await notifier.close()


@pytest.mark.asyncio
async def test_lost_wake_still_emits_twenty_minutes_after_reanchored_wake() -> None:
    """安全网：唤醒派发真丢失（节点带未读停死在停靠位）——重锚后 20 分钟照常报警。

    `_emit_if_still_due` 的桶值按真实墙钟计算，所以唤醒时刻设在 21 分钟前。
    """
    service = _ReanchorFakeService(designed_idle=False)
    wake_at = datetime.now(timezone.utc) - timedelta(minutes=21)
    service._clock = wake_at
    standby_anchor = (wake_at - timedelta(minutes=25)).isoformat()
    service.state["last_visible_output_at"] = standby_anchor
    service.state[STALL_STANDBY_DISARMED_META_KEY] = standby_anchor
    notifier = TaskStallNotifier(service=service)
    try:
        notifier.start_task("task:standby")
        meta = service.log_service.read_task_runtime_meta("task:standby")
        assert meta["last_visible_output_at"] == wake_at.isoformat(timespec="microseconds")
        assert service.emitted == []

        # 唤醒通知从未被消费（未读一直在），节点停在停靠相位：现在（唤醒后 21 分钟）必须报警。
        await notifier._emit_if_still_due("task:standby")
        assert service.emitted, "a genuinely lost wake must still be reported"
        assert service.emitted[0]["bucket_minutes"] == 20
        assert service.emitted[0]["last_visible_output_at"] == wake_at.isoformat(timespec="microseconds")
        meta = service.log_service.read_task_runtime_meta("task:standby")
        assert int(meta.get("last_stall_notice_bucket_minutes") or 0) == 20
    finally:
        await notifier.close()


@pytest.mark.asyncio
async def test_reset_visible_output_clears_standby_flag_and_start_task_keeps_fresh_anchor() -> None:
    """可见输出即待命结束：标记清掉；无标记的 start_task 不重锚（保住非待命语义）。"""
    service = _ReanchorFakeService(designed_idle=False)
    _mark_standby(service.state, minutes_ago=25)
    notifier = TaskStallNotifier(service=service)
    try:
        fresh_anchor = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        notifier.reset_visible_output("task:standby", occurred_at=fresh_anchor)
        meta = service.log_service.read_task_runtime_meta("task:standby")
        assert meta["last_visible_output_at"] == fresh_anchor
        assert str(meta.get(STALL_STANDBY_DISARMED_META_KEY) or "") == ""

        # 无标记：start_task 保留刚复位的锚点，不做重锚。
        notifier.start_task("task:standby")
        meta = service.log_service.read_task_runtime_meta("task:standby")
        assert meta["last_visible_output_at"] == fresh_anchor
        assert service.emitted == []
    finally:
        await notifier.close()


@pytest.mark.asyncio
async def test_schedule_without_standby_does_not_write_the_flag() -> None:
    service = _ReanchorFakeService(designed_idle=False)
    notifier = TaskStallNotifier(service=service)
    try:
        service.state["last_visible_output_at"] = datetime.now(timezone.utc).isoformat()
        notifier._schedule("task:standby")
        assert "task:standby" in notifier._tasks
        assert str(service.state.get(STALL_STANDBY_DISARMED_META_KEY) or "") == ""
    finally:
        await notifier.close()


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def _runtime_service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )


@pytest.mark.asyncio
async def test_runtime_meta_persists_standby_flag_through_log_service(tmp_path: Path) -> None:
    """白名单合同：update_task_runtime_meta 必须放行 stall_standby_disarmed_at（读回一致）。"""
    service = _runtime_service(tmp_path)
    try:
        record = await service.create_task("standby flag roundtrip", session_id="web:standby-meta")
        stamped = "2026-11-07T07:41:50.123456+00:00"
        service.log_service.update_task_runtime_meta(
            record.task_id,
            stall_standby_disarmed_at=stamped,
        )
        meta = service.log_service.read_task_runtime_meta(record.task_id) or {}
        assert str(meta.get(STALL_STANDBY_DISARMED_META_KEY) or "") == stamped

        service.log_service.update_task_runtime_meta(
            record.task_id,
            stall_standby_disarmed_at="",
        )
        meta = service.log_service.read_task_runtime_meta(record.task_id) or {}
        assert str(meta.get(STALL_STANDBY_DISARMED_META_KEY) or "") == ""
    finally:
        await service.close()
