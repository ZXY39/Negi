"""mention_member：目标解析、原始 openid 拒绝与 outbound mentions 契约。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import main  # noqa: F401  (production import order)
from g3ku.agent.tools.mention_member import MentionMemberTool
from g3ku.runtime.frontdoor import _ceo_runtime_ops as runtime_ops


class _Bus:
    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish_outbound(self, message) -> None:
        self.published.append(message)


class _Registry:
    def __init__(self, entry) -> None:
        self.entry = entry

    def get_by_session_key(self, session_key):
        return self.entry

    def active_message_state(self, session_key):
        return ""

    def last_inbound_at(self, session_key):
        return ""


@pytest.mark.asyncio
async def test_mention_tool_passes_runtime_to_publisher() -> None:
    calls: list[dict] = []

    async def publish(**kwargs):
        calls.append(dict(kwargs))
        return {"ok": True, "status": "queued"}

    tool = MentionMemberTool(publish)
    result = await tool.execute(
        "Master", "请确认", __g3ku_runtime={"session_key": "ext:qq-official-1:abc"}
    )
    assert json.loads(result)["ok"] is True
    assert calls[0]["target"] == "Master"
    assert calls[0]["runtime"]["session_key"] == "ext:qq-official-1:abc"


def test_mention_roster_resolves_numbers_and_master(monkeypatch) -> None:
    history = [
        {
            "role": "user",
            "content": "a",
            "metadata": {
                "external_trust": "third_party_group",
                "external_sender": {"id": "AAAA"},
            },
        },
        {
            "role": "user",
            "content": "b",
            "metadata": {
                "external_trust": "third_party_group",
                "external_sender": {"id": "OWNER"},
            },
        },
    ]
    session = SimpleNamespace(messages=history)
    sessions = SimpleNamespace(get_or_create=lambda key: session)
    fake_self = SimpleNamespace(_loop=SimpleNamespace(sessions=sessions))
    monkeypatch.setattr(runtime_ops, "qq_owner_openids", lambda: frozenset({"OWNER"}))

    by_number, master_id, roster = runtime_ops.CeoFrontDoorRuntimeOps._mention_roster(
        fake_self, "ext:qq-official-1:test-mention-roster"
    )
    assert by_number == {2: "AAAA"}
    assert master_id == "OWNER"
    assert roster.resolve("master")["id"] == "OWNER"


class _FakeRoster:
    def resolve(self, target: str):
        raw = str(target or "").strip()
        if raw.lower() == "master":
            return {"ok": True, "id": "OWNER", "number": 1, "name": "Master"}
        if raw in {"0002", "2", "Alice"}:
            return {"ok": True, "id": "AAAA", "number": 2, "name": "Alice", "role": "member"}
        return {"ok": False, "reason": "target_not_found"}


@pytest.mark.asyncio
async def test_publish_mention_targets_roster_and_rejects_raw_openid(monkeypatch) -> None:
    bus = _Bus()
    entry = SimpleNamespace(
        session_key="ext:qq-official-1:abc",
        external_key="qq:group:g1",
        chat_type="group",
    )
    fake_self = SimpleNamespace(
        _loop=SimpleNamespace(bus=bus),
        _mention_roster=lambda key: ({2: "AAAA"}, "OWNER", _FakeRoster()),
    )
    published: list[dict] = []

    async def _publish(bus_arg, session_key, text, *, source, metadata=None):
        published.append(
            {"bus": bus_arg, "session_key": session_key, "text": text, "metadata": dict(metadata or {})}
        )
        return True

    monkeypatch.setattr(runtime_ops, "get_external_session_registry", lambda: _Registry(entry))
    monkeypatch.setattr(runtime_ops, "publish_channel_notice", _publish)

    master = await runtime_ops.CeoFrontDoorRuntimeOps._frontdoor_publish_mention(
        fake_self,
        target="Master",
        message="请确认",
        runtime={"session_key": entry.session_key},
    )
    assert master["ok"] is True
    assert published[-1]["metadata"]["mentions"] == ["OWNER"]
    assert bus.published == []  # publish_channel_notice is the injected lane

    runtime_ops._MENTION_SEND_TIMES.clear()
    third = await runtime_ops.CeoFrontDoorRuntimeOps._frontdoor_publish_mention(
        fake_self,
        target="0002",
        message="提醒一下",
        runtime={"session_key": entry.session_key},
    )
    assert third["ok"] is True
    assert published[-1]["metadata"]["mentions"] == ["AAAA"]

    rejected = await runtime_ops.CeoFrontDoorRuntimeOps._frontdoor_publish_mention(
        fake_self,
        target="ABCDEF1234567890",
        message="hi",
        runtime={"session_key": entry.session_key},
    )
    assert rejected["ok"] is False
    assert rejected["reason"] == "raw_openid_not_allowed"


@pytest.mark.asyncio
async def test_publish_mention_accepts_name_and_multiple_targets(monkeypatch) -> None:
    bus = _Bus()
    entry = SimpleNamespace(
        session_key="ext:qq-official-1:multi",
        external_key="qq:group:g1",
        chat_type="group",
    )
    fake_self = SimpleNamespace(
        _loop=SimpleNamespace(bus=bus),
        _mention_roster=lambda key: ({2: "AAAA"}, "OWNER", _FakeRoster()),
    )
    published: list[dict] = []

    async def _publish(bus_arg, session_key, text, *, source, metadata=None):
        published.append({"metadata": dict(metadata or {})})
        return True

    monkeypatch.setattr(runtime_ops, "get_external_session_registry", lambda: _Registry(entry))
    monkeypatch.setattr(runtime_ops, "publish_channel_notice", _publish)
    runtime_ops._MENTION_SEND_TIMES.clear()

    result = await runtime_ops.CeoFrontDoorRuntimeOps._frontdoor_publish_mention(
        fake_self,
        targets=["Alice", "Master", "0002"],
        message="请两位确认",
        runtime={"session_key": entry.session_key},
    )
    assert result["ok"] is True
    assert published[-1]["metadata"]["mentions"] == ["AAAA", "OWNER"]

    runtime_ops._MENTION_SEND_TIMES.clear()
    failed = await runtime_ops.CeoFrontDoorRuntimeOps._frontdoor_publish_mention(
        fake_self,
        target="Nobody",
        message="hi",
        runtime={"session_key": entry.session_key},
    )
    assert failed["ok"] is False
    assert failed["reason"] == "target_resolution_failed"
    assert failed["results"][0]["reason"] == "target_not_found"
