"""Session-link addressing and delivery contract: aliases, ACL, dedupe, chain/hop."""

from __future__ import annotations

import asyncio

import pytest

from main.models import FrontdoorSessionMessage
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore


@pytest.fixture()
def service(tmp_path):
    instance = MainRuntimeService.__new__(MainRuntimeService)
    instance.store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    instance.replace_session_links("web:ceo-a", ["web:ceo-b", "web:ceo-c"])
    yield instance
    instance.store.close()


def _send(service, sender, to, content="hello", **kwargs):
    return asyncio.run(
        service.deliver_frontdoor_node_message(
            session_id=sender, to=to, content=content, **kwargs
        )
    )


def test_bare_alias_and_explicit_prefix_resolve(service):
    alias_b = service.get_session_alias("web:ceo-b")
    for target in (alias_b, f"session:{alias_b}", "session:web:ceo-b"):
        result = _send(service, "web:ceo-a", target, content=f"to {target}")
        assert result["ok"] is True, result
        item = result["results"][0]
        assert item["status"] == "delivered"
        assert item["target_kind"] == "session"
        assert item["resolved_session_key"] == "web:ceo-b"
        assert item["wake"] == "session_inbox"


def test_one_to_many_list_per_target_receipts(service):
    alias_b = service.get_session_alias("web:ceo-b")
    alias_c = service.get_session_alias("web:ceo-c")
    result = _send(service, "web:ceo-a", [alias_b, alias_c])
    assert result["ok"] is True
    assert [item["resolved_session_key"] for item in result["results"]] == [
        "web:ceo-b",
        "web:ceo-c",
    ]
    assert len(service.store.list_pending_frontdoor_session_messages()) == 2


def test_unlinked_target_rejected_with_legal_aliases(service):
    service.replace_session_links("web:ceo-d", [])
    result = _send(service, "web:ceo-d", "s1")
    assert result["ok"] is False
    item = result["results"][0]
    assert item["reason"] == "session_not_linked"
    # No links for d: legal targets carry no session aliases.
    assert result["legal_targets"] == []
    # a has links; a rejection surfaces its reachable aliases.
    result = _send(service, "web:ceo-a", "session:web:ceo-zzz")
    assert result["results"][0]["reason"] == "session_not_linked"
    assert service.get_session_alias("web:ceo-b") in result["legal_targets"]


def test_unknown_alias_and_bad_shape_rejected(service):
    result = _send(service, "web:ceo-a", "s999")
    assert result["results"][0]["reason"] == "session_missing"
    result = _send(service, "web:ceo-a", "session:not-a-key")
    assert result["results"][0]["reason"] == "session_missing"
    # A bare token that names nothing keeps the node/task rejection code.
    result = _send(service, "web:ceo-a", "deadbeef0000")
    assert result["results"][0]["reason"] == "target_not_in_task"


def test_self_send_rejected(service):
    alias_a = service.get_session_alias("web:ceo-a")
    result = _send(service, "web:ceo-a", alias_a)
    assert result["results"][0]["reason"] == "session_not_linked"


def test_reply_to_inherits_chain_and_hop(service):
    first = _send(service, "web:ceo-a", "s2")["results"][0]
    assert first["hop"] == 0
    assert first["chain_id"] == first["message_id"]
    reply = _send(
        service, "web:ceo-b", "s1", content="answer", reply_to=first["message_id"]
    )["results"][0]
    assert reply["status"] == "delivered"
    assert reply["hop"] == 1
    assert reply["chain_id"] == first["chain_id"]
    stored = service.store.get_frontdoor_session_message(reply["message_id"])
    assert stored.payload["reply_to"] == first["message_id"]
    assert stored.payload["wakeable"] is True


def test_reply_to_must_be_own_incoming_message(service):
    first = _send(service, "web:ceo-a", "s2")["results"][0]
    # c never received that message (it went to b).
    result = _send(service, "web:ceo-c", "s1", reply_to=first["message_id"])
    assert result["results"][0]["reason"] == "reply_to_unknown"
    result = _send(service, "web:ceo-b", "s1", reply_to="smsg:missing")
    assert result["results"][0]["reason"] == "reply_to_unknown"


def test_hop_cap_lands_without_wake(service):
    service.store.upsert_frontdoor_session_message(
        FrontdoorSessionMessage(
            message_id="smsg:deep",
            session_id="web:ceo-b",
            source_session_key="web:ceo-a",
            content="deep",
            created_at="t0",
            payload={"chain_id": "chain:x", "hop": 7, "fingerprint": "fpdeep"},
        )
    )
    reply = _send(service, "web:ceo-b", "s1", content="cap", reply_to="smsg:deep")["results"][0]
    assert reply["status"] == "delivered"
    assert reply["hop"] == 8
    assert reply["wake"] == "session_inbox_no_wake"
    assert "wake budget" in reply["notice"]
    stored = service.store.get_frontdoor_session_message(reply["message_id"])
    assert stored.payload["wakeable"] is False
    # A fresh message without reply_to starts a new chain at hop 0.
    fresh = _send(service, "web:ceo-b", "s1", content="new topic")["results"][0]
    assert fresh["hop"] == 0
    assert fresh["wake"] == "session_inbox"


def test_fingerprint_dedupe_and_consumed_resend(service):
    first = _send(service, "web:ceo-a", "s2", content="same")["results"][0]
    again = _send(service, "web:ceo-a", "s2", content="same")["results"][0]
    assert again["status"] == "deduplicated"
    assert again["message_id"] == first["message_id"]
    service.store.mark_frontdoor_session_message_consumed(first["message_id"], consumed_at="t1")
    third = _send(service, "web:ceo-a", "s2", content="same")["results"][0]
    assert third["status"] == "delivered"
    assert third["message_id"] != first["message_id"]


def test_sender_alias_stamped_in_payload(service):
    result = _send(service, "web:ceo-a", "s2")["results"][0]
    stored = service.store.get_frontdoor_session_message(result["message_id"])
    assert stored.payload["source_alias"] == service.get_session_alias("web:ceo-a")
    assert stored.source_session_key == "web:ceo-a"
