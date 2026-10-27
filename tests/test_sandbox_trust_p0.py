"""P0 信任分级 + 记忆门 + 只读档 + 读根限定。

契约见 ``work/sandbox-trust-plan.md``；架构口径见
``docs/architecture/sandbox-execution-plane.md``。这些断言是 P0 的验收面：
每一处绕过都对应一条真实可用的攻击路径，不是"实现细节"。
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

# 先导入 main：`session_keys` ↔ `main.runtime.react_loop` 的既有循环要求这个顺序
# （生产入口也是先装 main 再进 runtime）。
import main  # noqa: F401
from g3ku.agent.tools.filesystem import _resolve_path as resolve_fs_path
from g3ku.content.navigation import ContentNavigationService
from g3ku.runtime.sender_stamp import (
    MESSAGE_SENDER_MARKER,
    render_sender_stamp,
    strip_sender_stamp,
)
from g3ku.runtime.session_trust import (
    TRUST_OWNER,
    TRUST_THIRD_PARTY_DM,
    TRUST_THIRD_PARTY_GROUP,
    is_trusted_sender,
    more_restrictive,
    resolve_turn_trust,
    session_trust,
)
from g3ku.sandbox.policy import (
    INSTALL_NOT_INSTALLED,
    INSTALL_READY,
    MODE_RESTRICTED,
    MODE_UNRESTRICTED,
    TIER_HOST_FULL,
    TIER_JAILED,
    TIER_READONLY,
    default_mode_for_trust,
    resolve_tier,
    session_policy,
)
from g3ku.utils.path_roots import (
    current_read_roots,
    enforce_roots,
    normalize_roots,
    pinned_read_roots,
)


@pytest.fixture()
def registry(tmp_path: Path):
    from g3ku.runtime.external_sessions import (
        get_external_session_registry,
        reset_external_session_registry,
    )

    reset_external_session_registry()
    reg = get_external_session_registry(tmp_path)
    try:
        yield reg
    finally:
        reset_external_session_registry()


# -- 信任分级 ---------------------------------------------------------------


def test_group_chat_is_third_party_even_for_owner_sender() -> None:
    trust = resolve_turn_trust(
        chat_type="group",
        sender_id="owner-openid",
        owner_openids=["owner-openid"],
    )
    assert trust == TRUST_THIRD_PARTY_GROUP


def test_group_shape_can_also_come_from_external_key_prefix() -> None:
    trust = resolve_turn_trust(external_key="qq:group:abc", sender_id="owner-openid",
                               owner_openids=["owner-openid"])
    assert trust == TRUST_THIRD_PARTY_GROUP


def test_dm_owner_vs_stranger() -> None:
    assert resolve_turn_trust(
        chat_type="dm", sender_id="owner-openid", owner_openids=["owner-openid"]
    ) == TRUST_OWNER
    assert resolve_turn_trust(
        chat_type="dm", sender_id="stranger", owner_openids=["owner-openid"]
    ) == TRUST_THIRD_PARTY_DM
    assert resolve_turn_trust(
        chat_type="dm", sender_id="bridge-trusted", trusted_senders=["bridge-trusted"]
    ) == TRUST_OWNER


def test_empty_sender_is_never_owner() -> None:
    assert is_trusted_sender("", owner_openids=[""]) is False
    assert is_trusted_sender(None, trusted_senders=[""]) is False
    assert resolve_turn_trust(chat_type="dm", sender_id="") == TRUST_THIRD_PARTY_DM


def test_trust_only_moves_down() -> None:
    assert more_restrictive(TRUST_OWNER, TRUST_THIRD_PARTY_DM) == TRUST_THIRD_PARTY_DM
    assert more_restrictive(TRUST_THIRD_PARTY_DM, TRUST_OWNER) == TRUST_THIRD_PARTY_DM
    assert more_restrictive(TRUST_THIRD_PARTY_DM, TRUST_THIRD_PARTY_GROUP) == TRUST_THIRD_PARTY_GROUP
    assert more_restrictive("", TRUST_OWNER) == TRUST_OWNER


def test_registry_records_sticky_trust(registry) -> None:
    from g3ku.runtime.session_trust import note_inbound_session_shape

    entry, _ = registry.resolve_or_create(
        bridge_id="qq-official-1", external_key="qq:c2c:peer", chat_type="dm"
    )
    note_inbound_session_shape(entry.session_key, chat_type="dm", sender_id="stranger")
    # 后续一条自称操作员的消息不能把会话抬回去
    note_inbound_session_shape(
        entry.session_key, chat_type="dm", sender_id="owner", owner_openids=["owner"]
    )
    assert registry.get_by_session_key(entry.session_key).trust == TRUST_THIRD_PARTY_DM
    assert session_trust(entry.session_key) == TRUST_THIRD_PARTY_DM

    owner_entry, _ = registry.resolve_or_create(
        bridge_id="qq-official-1", external_key="qq:c2c:owner", chat_type="dm"
    )
    note_inbound_session_shape(
        owner_entry.session_key, chat_type="dm", sender_id="owner", owner_openids=["owner"]
    )
    assert session_trust(owner_entry.session_key) == TRUST_OWNER


def test_unknown_external_session_fails_closed(registry) -> None:
    assert session_trust("ext:unknown-bridge:deadbeef") == TRUST_THIRD_PARTY_DM
    assert session_trust("china:qq:acct:group:123") == TRUST_THIRD_PARTY_GROUP
    assert session_trust("web:ceo-1") == TRUST_OWNER
    assert session_trust("internal:heartbeat") == TRUST_OWNER


# -- 档位解析 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("trust", "mode", "install", "expected"),
    [
        (TRUST_OWNER, MODE_UNRESTRICTED, INSTALL_NOT_INSTALLED, TIER_HOST_FULL),
        (TRUST_OWNER, MODE_RESTRICTED, INSTALL_NOT_INSTALLED, TIER_READONLY),
        (TRUST_OWNER, MODE_RESTRICTED, INSTALL_READY, TIER_JAILED),
        (TRUST_THIRD_PARTY_GROUP, MODE_RESTRICTED, INSTALL_NOT_INSTALLED, TIER_READONLY),
        (TRUST_THIRD_PARTY_GROUP, MODE_RESTRICTED, INSTALL_READY, TIER_JAILED),
        (TRUST_THIRD_PARTY_GROUP, MODE_UNRESTRICTED, INSTALL_READY, TIER_HOST_FULL),
    ],
)
def test_tier_matrix(trust: str, mode: str, install: str, expected: str) -> None:
    # `jail_available=True` 模拟 P1a 后端接进执行道之后的状态；默认（未接后端时）
    # 永不发放 jailed，见 test_jailed_is_never_granted_without_backends。
    assert (
        resolve_tier(trust=trust, mode=mode, install_state=install, jail_available=True)
        == expected
    )


def test_default_mode_follows_trust() -> None:
    assert default_mode_for_trust(TRUST_OWNER) == MODE_UNRESTRICTED
    assert default_mode_for_trust(TRUST_THIRD_PARTY_GROUP) == MODE_RESTRICTED
    assert default_mode_for_trust(TRUST_THIRD_PARTY_DM) == MODE_RESTRICTED


def test_third_party_readonly_is_a_whitelist(registry) -> None:
    entry, _ = registry.resolve_or_create(
        bridge_id="qq-official-1", external_key="qq:group:g1", chat_type="group"
    )
    policy = session_policy(entry.session_key)
    assert policy.tier == TIER_READONLY
    for allowed in (
        "silent",
        "time",
        "load_tool_context",
        "load_skill_context",
        "content_open",
        "content_search",
        "content_describe",
        "filesystem_stat",
        "view_message_history",
    ):
        assert policy.tool_allowed(allowed), allowed
    for denied in (
        "exec",
        "process_kill",
        "filesystem_write",
        "filesystem_delete",
        "web_fetch",
        "skill-installer",
        "memory_update",
        "create_async_task",
        "task_summary",
        "send_message",
        "cron",
        "schedule_wake",
        "some_future_tool",
    ):
        assert not policy.tool_allowed(denied), denied


def test_owner_readonly_is_a_denylist() -> None:
    policy = session_policy("web:ceo-1", mode=MODE_RESTRICTED)
    assert policy.tier == TIER_READONLY
    assert policy.exec_requires_governed is True
    # 防手滑：干不了写/进程/任务，但读与联网照常
    assert policy.tool_allowed("exec")
    assert policy.tool_allowed("web_fetch")
    assert policy.tool_allowed("content_open")
    assert policy.tool_allowed("some_skill_tool")
    for denied in (
        "filesystem_write",
        "filesystem_edit",
        "filesystem_propose_patch",
        "process_kill",
        "skill-installer",
        "create_async_task",
        "task_summary",
        "send_message",
        "cron",
        "schedule_wake",
    ):
        assert not policy.tool_allowed(denied), denied


def test_owner_unrestricted_keeps_today_behavior() -> None:
    policy = session_policy("web:ceo-1")
    assert policy.tier == TIER_HOST_FULL
    for name in ("exec", "filesystem_write", "create_async_task", "memory_update", "web_fetch"):
        assert policy.tool_allowed(name), name
    assert policy.exec_requires_governed is False


def test_empty_session_key_is_not_restricted() -> None:
    # 无会话上下文的装配（测试/CLI）不该被当成第三方
    policy = session_policy("")
    assert policy.tier == TIER_HOST_FULL


# -- 读根限定 ---------------------------------------------------------------


def test_path_roots_enforce_and_block_symlink_escape(tmp_path: Path) -> None:
    inside = tmp_path / "session"
    inside.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    roots = normalize_roots([inside])

    ok = (inside / "a.txt")
    ok.write_text("ok", encoding="utf-8")
    assert enforce_roots(ok.resolve(), roots) == ok.resolve()

    with pytest.raises(PermissionError):
        enforce_roots(secret.resolve(), roots)

    link = inside / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not available on this platform")
    with pytest.raises(PermissionError):
        enforce_roots(link.resolve(), roots)


def test_pinned_read_roots_scope_tool_resolution(tmp_path: Path) -> None:
    inside = tmp_path / "session"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("x", encoding="utf-8")

    # 没有 pin：不设限
    assert current_read_roots() == ()
    assert resolve_fs_path(str(outside / "x.txt"), workspace=tmp_path) == (outside / "x.txt").resolve()

    with pinned_read_roots([inside]):
        assert current_read_roots() == (inside.resolve(),)
        with pytest.raises(PermissionError):
            resolve_fs_path(str(outside / "x.txt"), workspace=tmp_path)
        target = inside / "ok.txt"
        target.write_text("ok", encoding="utf-8")
        assert resolve_fs_path(str(target), workspace=tmp_path) == target.resolve()

    assert current_read_roots() == ()


def test_content_navigation_denies_outside_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    session_dir = workspace / ".g3ku" / "external-uploads" / "s1"
    session_dir.mkdir(parents=True)
    host_secret = workspace / "config.json"
    host_secret.write_text('{"secret": true}', encoding="utf-8")

    service = ContentNavigationService(workspace=workspace, allowed_roots=[session_dir])
    readable = session_dir / "note.txt"
    readable.write_text("hello", encoding="utf-8")
    payload = service.open(path=str(readable))
    assert "hello" in json.dumps(payload, ensure_ascii=False)

    with pytest.raises(PermissionError):
        service.open(path=str(host_secret))


# -- 投影标注 ---------------------------------------------------------------


def test_sender_stamp_roundtrip() -> None:
    metadata = {
        "external_trust": TRUST_THIRD_PARTY_GROUP,
        "external_sender": {"id": "0123456789abcdef", "name": ""},
        "chat_type": "group",
    }
    stamp = render_sender_stamp(metadata)
    assert stamp.startswith(MESSAGE_SENDER_MARKER)
    assert stamp.endswith("\n")
    assert "群消息" in stamp
    assert "第三方" in stamp

    body = "帮我跑一下 dir"
    stamped = f"{stamp}{body}"
    assert strip_sender_stamp(stamped) == body
    # 用户正文里恰好出现标记文本时不误删
    plain = f"我说的是 {MESSAGE_SENDER_MARKER} 这行字"
    assert strip_sender_stamp(plain) == plain


def test_sender_stamp_skips_owner_and_missing_trust() -> None:
    assert render_sender_stamp({"external_trust": TRUST_OWNER}) == ""
    assert render_sender_stamp({}) == ""
    assert render_sender_stamp(None) == ""


def test_projection_adds_stamp_but_keeps_content_raw() -> None:
    from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder

    message = {
        "role": "user",
        "content": "帮我看下这个文件",
        "timestamp": "2026-10-27T01:00:00+08:00",
        "metadata": {
            "external_trust": TRUST_THIRD_PARTY_GROUP,
            "external_sender": {"id": "member_openid_1", "name": ""},
            "chat_type": "group",
        },
    }
    entry = CeoMessageBuilder._history_message(message)
    assert entry["content"].startswith(MESSAGE_SENDER_MARKER)
    assert "帮我看下这个文件" in entry["content"]
    assert strip_sender_stamp(entry["content"]).startswith("帮我看下这个文件")
    # 转录原文不动
    assert message["content"] == "帮我看下这个文件"


# -- 控制面拒绝（approval 闸门） -------------------------------------------


def test_approval_gate_auto_rejects_third_party_tools(registry) -> None:
    from types import SimpleNamespace

    from g3ku.runtime.frontdoor import CeoFrontDoorRunner

    entry, _ = registry.resolve_or_create(
        bridge_id="qq-official-1", external_key="qq:group:g1", chat_type="group"
    )
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    result = runner._approval_request_for_tool_calls(
        [
            {"id": "c1", "name": "exec", "arguments": {"command": "dir"}},
            {"id": "c2", "name": "content_open", "arguments": {"path": "note.txt"}},
            {"id": "c3", "name": "future_unknown_tool", "arguments": {}},
        ],
        session_key=entry.session_key,
    )
    assert result is not None
    # 第三方会话没有审批面，策略也不落中断：整批走合成拒绝车道
    assert result["auto_reject"] is True
    assert result["channel_session"] is True
    assert result["policy_tier"] == TIER_READONLY
    assert [item["tool_call_id"] for item in result["review_items"]] == ["c1", "c3"]
    assert all(item["reason"] == "trust_policy" for item in result["review_items"])
    assert result["pass_through_tool_call_ids"] == ["c2"]


# -- 记忆门 -----------------------------------------------------------------


def _memory_cfg():
    from g3ku.config.schema import MemoryToolsConfig

    payload = MemoryToolsConfig().model_dump(mode="python")
    payload["document"] = {
        "summary_max_chars": 250,
        "document_max_chars": 20000,
        "memory_file": "memory/MEMORY.md",
        "notes_dir": "memory/notes",
    }
    payload["queue"] = {
        "queue_file": "memory/queue.jsonl",
        "ops_file": "memory/ops.jsonl",
        "batch_max_chars": 50,
        "max_wait_seconds": 3,
        "review_interval_turns": 2,
    }
    return MemoryToolsConfig.model_validate(payload)


def _memory_module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


@pytest.mark.asyncio
async def test_memory_gate_ignores_untrusted_session(tmp_path: Path) -> None:
    module = _memory_module()
    manager = module.MemoryManager(
        tmp_path,
        _memory_cfg(),
        review_eligibility=lambda key: key.startswith("web:"),
    )
    try:
        result = await manager.record_turn_for_review(
            session_key="ext:qq-official-1:abc",
            turn_id="t1",
            user_messages=["记住我喜欢 X"],
            assistant_text="好的",
        )
        assert result["status"] == "ignored"
        assert result["reason"] == "untrusted_session"
        state = json.loads((tmp_path / "memory" / "review_state.json").read_text(encoding="utf-8"))
        assert "ext:qq-official-1:abc" not in (state.get("sessions") or {})
        assert not (tmp_path / "memory" / "queue.jsonl").read_text(encoding="utf-8").strip()

        # 合格会话照常入缓冲
        ok = await manager.record_turn_for_review(
            session_key="web:ceo-1",
            turn_id="t2",
            user_messages=["记住我喜欢 Y"],
            assistant_text="好的",
        )
        assert ok["status"] == "buffered"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_memory_gate_flush_discards_and_clears_buffer(tmp_path: Path) -> None:
    module = _memory_module()
    manager = module.MemoryManager(
        tmp_path,
        _memory_cfg(),
        review_eligibility=lambda key: True,
    )
    try:
        # 先以"合格"身份缓冲一轮，再模拟信任降级后的冲刷
        await manager.record_turn_for_review(
            session_key="ext:late-third-party",
            turn_id="t1",
            user_messages=["先记住这个"],
            assistant_text="好的",
        )
        manager._review_eligibility = lambda key: False
        result = await manager.flush_review_window(
            session_key="ext:late-third-party", trigger_source="session_boundary"
        )
        assert result["status"] == "discarded"
        assert result["reason"] == "untrusted_session"
        state = json.loads((tmp_path / "memory" / "review_state.json").read_text(encoding="utf-8"))
        assert "ext:late-third-party" not in (state.get("sessions") or {})
        assert not (tmp_path / "memory" / "queue.jsonl").read_text(encoding="utf-8").strip()
    finally:
        manager.close()


def test_memory_queue_partition_splits_by_trust(tmp_path: Path) -> None:
    module = _memory_module()
    manager = module.MemoryManager(
        tmp_path,
        _memory_cfg(),
        review_eligibility=lambda key: key.startswith("web:"),
    )
    try:
        good = module.MemoryQueueRequest(
            op="write",
            decision_source="self",
            payload_text="ok",
            created_at="2026-10-27T00:00:00+08:00",
            request_id="r1",
            session_key="web:ceo-1",
        )
        bad = module.MemoryQueueRequest(
            op="write",
            decision_source="self",
            payload_text="poison",
            created_at="2026-10-27T00:00:01+08:00",
            request_id="r2",
            session_key="ext:qq-official-1:abc",
        )
        batch, dropped = manager._partition_queue_batch_by_eligibility(
            module.MemoryBatch(op="write", items=[good, bad])
        )
        assert [item.request_id for item in batch.items] == ["r1"]
        assert [item.request_id for item in dropped] == ["r2"]
        assert manager._failed_record_all_untrusted(
            {"items": [{"session_key": "ext:qq-official-1:abc"}]}
        )
        assert not manager._failed_record_all_untrusted(
            {"items": [{"session_key": "ext:x:1"}, {"session_key": "web:ceo-1"}]}
        )
    finally:
        manager.close()


def test_memory_boot_sweep_drops_untrusted_buffer(tmp_path: Path) -> None:
    module = _memory_module()
    first = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        import asyncio

        asyncio.run(
            first.record_turn_for_review(
                session_key="ext:qq-official-1:abc",
                turn_id="t1",
                user_messages=["群里说的话"],
                assistant_text="收到",
            )
        )
        state = json.loads((tmp_path / "memory" / "review_state.json").read_text(encoding="utf-8"))
        assert "ext:qq-official-1:abc" in (state.get("sessions") or {})
    finally:
        first.close()

    second = module.MemoryManager(
        tmp_path,
        _memory_cfg(),
        review_eligibility=lambda key: not key.startswith("ext:"),
    )
    try:
        state = json.loads((tmp_path / "memory" / "review_state.json").read_text(encoding="utf-8"))
        assert "ext:qq-official-1:abc" not in (state.get("sessions") or {})
    finally:
        second.close()
