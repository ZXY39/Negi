"""停滞通知的送达计数：同一静默窗口里这是第几次送达，由服务端读数给出。

实盘里模型唯一能数出"第 5 次"的办法是数上下文里的副本；把旧副本折掉之后那条路不成立，
所以序号必须由注入侧从任务运行元数据算出来并印进束里。锚点一变就是新一次静默窗口，
计数从 1 重起——锚点与计数成对存放，不需要单独的清零通路。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.prompt_lane import _task_stall_lines
from g3ku.heartbeat.session_events import SessionHeartbeatEventQueue
from g3ku.heartbeat.session_service import WebSessionHeartbeatService
from g3ku.session.manager import SessionManager
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called here: {kwargs!r}")


class _FakeHeartbeatSession:
    def __init__(self, *, output: str = "") -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompts: list[UserInputMessage] = []
        self._listeners = set()
        self._output = output

    def subscribe(self, listener):
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    async def prompt(self, user_message, persist_transcript: bool = False) -> SimpleNamespace:
        _ = persist_transcript
        self.prompts.append(user_message)
        return SimpleNamespace(output=self._output)

    async def prompt_heartbeat(self, user_message, *, on_injected=None, already_injected: bool = False):
        """有注入边界的替身：消费点（`_on_injected`）只有走这条道才会被调用。

        旧替身 `prompt()` 走的是兼容分支（`session_service.py:1901-1903`），
        它不会通知注入成功，所以送达计数在那条路上永远不落账。
        """
        _ = already_injected
        self.prompts.append(user_message)
        result = SimpleNamespace(output=self._output, is_silent_reply=bool(self._output == ""))
        if callable(on_injected):
            await on_injected(result)
        return result


class _RuntimeManager:
    def __init__(self, session) -> None:
        self._session = session

    def get_or_create(self, **kwargs):
        _ = kwargs
        return self._session


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


def _service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )


def _anchor(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _stall_event(task_id: str, anchor: str, bucket: int, session_id: str):
    return SessionHeartbeatEventQueue().enqueue(
        session_id=session_id,
        source="main_runtime",
        reason="task_stall",
        dedupe_key=f"task-stall:{task_id}:{bucket}:{anchor}",
        payload={
            "task_id": task_id,
            "session_id": session_id,
            "title": "stall counting",
            "reason": "suspected_stall",
            "stalled_minutes": bucket,
            "bucket_minutes": bucket,
            "last_visible_output_at": anchor,
        },
        delay_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_delivery_index_rises_in_the_same_window_and_restarts_with_a_new_anchor(
    tmp_path: Path,
) -> None:
    """同锚点两次注入 → 1、2；锚点换了 → 回到 1；没注入成功不落账，序号不跳号。"""
    service = _service(tmp_path)
    try:
        record = await service.create_task("stall counting", session_id="web:ceo-count")
        task_id = record.task_id
        heartbeat = WebSessionHeartbeatService(
            workspace=tmp_path,
            agent=SimpleNamespace(tool_execution_manager=None),
            runtime_manager=_RuntimeManager(_FakeHeartbeatSession()),
            main_task_service=service,
            session_manager=SessionManager(tmp_path),
        )
        anchor = _anchor(45)

        first = _stall_event(task_id, anchor, 40, "web:ceo-count")
        assert heartbeat._enrich_event_payload_for_lane(first)["delivery_index"] == 1
        # 渲染不等于送达：只有注入成功那趟才落账
        assert heartbeat._enrich_event_payload_for_lane(first)["delivery_index"] == 1
        heartbeat._record_task_stall_deliveries([first])
        meta = service.log_service.read_task_runtime_meta(task_id) or {}
        assert int(meta.get("stall_delivery_count") or 0) == 1
        assert str(meta.get("stall_notice_anchor") or "") == anchor

        second = _stall_event(task_id, anchor, 50, "web:ceo-count")
        assert heartbeat._enrich_event_payload_for_lane(second)["delivery_index"] == 2
        heartbeat._record_task_stall_deliveries([second])
        meta = service.log_service.read_task_runtime_meta(task_id) or {}
        assert int(meta.get("stall_delivery_count") or 0) == 2

        # 节点重新产出可见输出 ⇒ 新的静默窗口，序号从 1 起
        new_anchor = _anchor(25)
        service.log_service.update_task_runtime_meta(task_id, last_visible_output_at=new_anchor)
        third = _stall_event(task_id, new_anchor, 20, "web:ceo-count")
        assert heartbeat._enrich_event_payload_for_lane(third)["delivery_index"] == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_injected_stall_bundle_prints_the_delivery_reading(tmp_path: Path) -> None:
    """车道出口的束体里必须带 `Delivery:` 那行，落账发生在注入之后。"""
    session_id = "web:ceo-print"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession()
    service = _service(tmp_path)
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    try:
        record = await service.create_task("stall print", session_id=session_id)
        anchor = _anchor(45)
        service.log_service.update_task_runtime_meta(
            record.task_id,
            last_visible_output_at=anchor,
            last_stall_notice_bucket_minutes=0,
        )
        heartbeat = WebSessionHeartbeatService(
            workspace=tmp_path,
            agent=SimpleNamespace(tool_execution_manager=None),
            runtime_manager=_RuntimeManager(live_session),
            main_task_service=service,
            session_manager=session_manager,
        )
        heartbeat.enqueue_task_stall_payload(
            service.build_task_stall_payload(
                record.task_id,
                bucket_minutes=40,
                last_visible_output_at=anchor,
            )
        )
        heartbeat._started = True

        await heartbeat._run_session(session_id)

        prompt = str(live_session.prompts[0].content)
        assert "Delivery: 1 notice(s) for this silent window" in prompt
        meta = service.log_service.read_task_runtime_meta(record.task_id) or {}
        assert int(meta.get("stall_delivery_count") or 0) == 1
        assert str(meta.get("stall_notice_anchor") or "") == anchor
    finally:
        await service.close()


def test_no_delivery_reading_means_no_line() -> None:
    """读数缺失就不印这行，不填 0（0 会被读成"第一次"，而真实形态是"没读数"）。"""
    payload = {
        "task_id": "task:count",
        "title": "t",
        "reason": "suspected_stall",
        "stalled_minutes": 40,
        "bucket_minutes": 40,
        "last_visible_output_at": "2026-11-02T09:33:36+00:00",
        "brief_text": "b",
        "latest_node_summary": "n",
        "runtime_summary_excerpt": "r",
    }
    without = "\n".join(_task_stall_lines(dict(payload), []))
    assert "Delivery:" not in without
    with_reading = "\n".join(_task_stall_lines({**payload, "delivery_index": 3}, []))
    assert "Delivery: 3 notice(s) for this silent window" in with_reading
