"""渠道会话删除的两个轴：默认只清历史、保留桥的身份映射；显式 `forget_identity`
才摘除身份，而摘除前必须证明没有东西还指着这个会话键（未 ack 的出站账本行、
指向它的定时任务），否则推送会静默消失——`find_by_any_key` 只 WARNING 后取最新。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from g3ku.cron.types import CronJob, CronPayload
from g3ku.runtime.api import ceo_sessions
from g3ku.runtime.external_outbox import (
    configure_external_outbox_root,
    record_outbound_message,
)
from g3ku.runtime.external_sessions import (
    get_external_session_registry,
    reset_external_session_registry,
)
from g3ku.session.manager import SessionManager

EXTERNAL_KEY = "qq:group:42"


class _FakeRuntimeManager:
    def __init__(self) -> None:
        self.removed: list[str] = []

    def remove(self, session_key: str) -> None:
        self.removed.append(session_key)


def _agent(cron_jobs) -> SimpleNamespace:
    cron_service = None
    if cron_jobs is not None:
        def _list_jobs(include_disabled: bool = False):
            jobs = list(cron_jobs)
            if not include_disabled:
                jobs = [job for job in jobs if getattr(job, "enabled", True)]
            return jobs

        cron_service = SimpleNamespace(list_jobs=_list_jobs)

    async def _cancel(session_key: str, *, reason: str = "") -> None:
        return None

    return SimpleNamespace(cron_service=cron_service, memory_manager=None, cancel_session_tasks=_cancel)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """真实 SessionManager + 真实注册表 + 真实账本根，只把销毁性的邻居接缝换成假件。"""
    monkeypatch.setattr(ceo_sessions, "get_web_heartbeat_service", lambda agent: None)
    monkeypatch.setattr(ceo_sessions, "delete_web_ceo_session_artifacts", lambda **kwargs: None)
    configure_external_outbox_root(tmp_path / "outbox")

    session_manager = SessionManager(tmp_path)
    # 删除链路走的是进程内单例（真正在路由出站的就是它），用例必须看同一个对象。
    reset_external_session_registry()
    registry = get_external_session_registry(tmp_path)
    entry, created = registry.resolve_or_create(bridge_id="qq", external_key=EXTERNAL_KEY)
    assert created is True
    session_key = entry.session_key

    async def delete(cron_jobs=None, *, session_key_override: str | None = None, **kwargs):
        return await ceo_sessions._delete_single_ceo_session(
            _agent(cron_jobs),
            session_manager,
            _FakeRuntimeManager(),
            SimpleNamespace(delete_task_records_for_session=lambda key: 0),
            session_key=session_key_override or session_key,
            is_channel_session=session_key_override is None,
            delete_task_records=False,
            **kwargs,
        )

    yield delete, registry, session_key
    configure_external_outbox_root(None)
    reset_external_session_registry()


def _write_transcript(session_manager: SessionManager, session_key: str) -> None:
    session = session_manager.get_or_create(session_key)
    session.add_message("user", "第一轮")
    session_manager.save(session)


async def test_delete_keeps_identity_by_default(harness):
    """既定契约：清历史不注销身份，桥下次用同一个 external_key 仍落回这个会话键。"""
    delete, registry, session_key = harness
    result = await delete()
    assert result["cleared"] is True
    assert result["identity_forgotten"] is False
    assert registry.get_by_session_key(session_key) is not None
    assert registry.get_session_key(bridge_id="qq", external_key=EXTERNAL_KEY) == session_key


async def test_forget_identity_drops_the_registry_row(harness):
    delete, registry, session_key = harness
    before = registry.get_by_session_key(session_key)
    result = await delete(forget_identity=True)
    assert result["identity_forgotten"] is True
    assert registry.get_by_session_key(session_key) is None
    assert registry.get_session_key(bridge_id="qq", external_key=EXTERNAL_KEY) is None
    # 会话键是 (bridge, external_key) 的确定性散列，所以同键再注册会拿回同一个路径——
    # 摘除的意义在于这是一条**新注册**（created=True、created_at 更新），旧身份不再被复用。
    entry, created = registry.resolve_or_create(bridge_id="qq", external_key=EXTERNAL_KEY)
    assert created is True
    assert entry.created_at != before.created_at


async def test_forget_identity_refused_while_reply_is_still_unacked(harness):
    delete, registry, session_key = harness
    record_outbound_message(
        session_key=session_key,
        external_key=EXTERNAL_KEY,
        text="还没被桥取走的答案",
        event="reply.final",
    )
    with pytest.raises(HTTPException) as raised:
        await delete(forget_identity=True)
    assert raised.value.status_code == 400
    assert raised.value.detail == "outbox_pending:1"
    assert registry.get_by_session_key(session_key) is not None


async def test_forget_identity_refused_when_job_targets_the_external_key(harness):
    delete, registry, session_key = harness
    job = CronJob(id="job-1", name="每日推送", enabled=True, payload=CronPayload(channel="ext", to=EXTERNAL_KEY))
    with pytest.raises(HTTPException) as raised:
        await delete([job], forget_identity=True)
    assert raised.value.status_code == 400
    assert raised.value.detail == "scheduled_target:job-1"
    assert registry.get_by_session_key(session_key) is not None


async def test_forget_identity_refused_when_job_targets_the_session_key(harness):
    delete, registry, session_key = harness
    job = CronJob(
        id="job-direct",
        name="直连会话键的推送",
        enabled=True,
        payload=CronPayload(session_key=session_key, channel="ext", to=EXTERNAL_KEY),
    )
    with pytest.raises(HTTPException) as raised:
        await delete([job], forget_identity=True)
    assert raised.value.detail == "scheduled_target:job-direct"


async def test_disabled_job_targeting_the_session_does_not_block(harness):
    delete, registry, session_key = harness
    job = CronJob(
        id="job-off",
        name="停掉的推送",
        enabled=False,
        payload=CronPayload(session_key=session_key, channel="ext", to=EXTERNAL_KEY),
    )
    result = await delete([job], forget_identity=True)
    assert result["identity_forgotten"] is True


async def test_default_delete_needs_no_gate(harness):
    """闸门只管身份：默认清历史这条道不能被新闸门打断。"""
    delete, registry, session_key = harness
    record_outbound_message(
        session_key=session_key, external_key=EXTERNAL_KEY, text="答案", event="reply.final"
    )
    result = await delete()
    assert result["cleared"] is True
    assert registry.get_by_session_key(session_key) is not None


async def test_forget_identity_retires_the_event_hub(harness):
    """同键再注册会落回同一个路径，所以缓冲必须跟着身份一起退役。"""
    from g3ku.runtime.external_events import get_session_event_hub

    delete, registry, session_key = harness
    get_session_event_hub(session_key).publish("reply.final", turn_id="old-life", text="上一世的回复")
    result = await delete(forget_identity=True)
    assert result["identity_forgotten"] is True
    hub = get_session_event_hub(session_key)
    assert hub.replay(0) == []
    assert hub.last_seq == 0


async def test_default_delete_keeps_the_event_hub(harness):
    """只清历史的语义不动缓冲：桥还在用同一条身份，重放语义不能变。"""
    from g3ku.runtime.external_events import get_session_event_hub

    delete, registry, session_key = harness
    get_session_event_hub(session_key).publish("reply.final", turn_id="keep-me", text="仍在账上的回复")
    await delete()
    hub = get_session_event_hub(session_key)
    assert [event["text"] for event in hub.replay(0)] == ["仍在账上的回复"]


async def test_local_session_forget_is_a_noop(harness):
    """本地 `web:` 会话没有外部身份可摘，旗标不该让它报错或误删别人的条目。"""
    delete, registry, _session_key = harness
    result = await delete(session_key_override="web:ceo-local", forget_identity=True)
    assert result["deleted"] is True
    assert result["identity_forgotten"] is False
    assert registry.get_by_session_key(_session_key) is not None


@pytest.mark.parametrize(
    ("route", "body", "expect"),
    [
        ("delete", {"forget_identity": True}, True),
        ("delete", {}, False),
        ("bulk", {"session_ids": ["ext:qq:b"], "forget_identity": True}, True),
        ("bulk", {"session_ids": ["ext:qq:b"]}, False),
    ],
)
def test_routes_pass_the_identity_flag_from_the_body(monkeypatch, route, body, expect):
    """HTTP 边只负责一件事：body 里的旗标必须真的落到删除链路，不能被静默丢掉。"""
    seen: list[bool] = []

    async def _fake_delete(*_args, **kwargs):
        seen.append(bool(kwargs.get("forget_identity")))
        return {"session_id": "ext:qq:b", "deleted": False, "cleared": True, "deleted_task_count": 0}

    async def _fake_service(_agent_value):
        return SimpleNamespace()

    monkeypatch.setattr(ceo_sessions, "_delete_single_ceo_session", _fake_delete)
    monkeypatch.setattr(
        ceo_sessions,
        "_sessions",
        lambda: (
            SimpleNamespace(cron_service=None),
            SimpleNamespace(),
            _FakeRuntimeManager(),
            SimpleNamespace(set_active_session_id=lambda session_id: None),
        ),
    )
    monkeypatch.setattr(ceo_sessions, "_task_service", _fake_service)
    monkeypatch.setattr(ceo_sessions, "_resolve_bulk_session_key", lambda *_a, **_k: ("ext:qq:b", True))
    monkeypatch.setattr(ceo_sessions, "resolve_active_ceo_session_id", lambda *_a, **_k: "")
    monkeypatch.setattr(ceo_sessions, "_build_catalog", lambda *_a, **_k: {"items": [], "channel_groups": []})
    monkeypatch.setattr(ceo_sessions, "store_ceo_catalog_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(ceo_sessions, "_publish_ceo_sessions_snapshot", lambda *_a, **_k: None)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    client = TestClient(app)

    if route == "delete":
        response = client.request("DELETE", "/api/ceo/sessions/ext:qq:b", json=body)
    else:
        response = client.post("/api/ceo/sessions/bulk-delete", json=body)

    assert response.status_code == 200, response.text
    assert seen == [expect]
