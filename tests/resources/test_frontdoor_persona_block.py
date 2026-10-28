from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.agent.persona import persona_digest, render_persona_notice
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.frontdoor.message_builder import (
    CeoMessageBuilder,
    adopt_persona,
)
from g3ku.runtime.skill_roster_notice import (
    clear_skill_roster_notice_stores,
    deliver_for_scope,
    notice_context_for,
    reset_persona_cursor,
)


class _PromptBuilder:
    def build(self, *, skills: list) -> str:
        return "BASE PROMPT"


class _PersonaManager:
    def __init__(self, *, document: str = "", persona: str = "") -> None:
        self.document = document
        self.persona = persona
        self.persona_reads = 0

    def snapshot_text(self, **_kwargs):
        return self.document

    def persona_text(self) -> str:
        self.persona_reads += 1
        return self.persona


def _loop(manager: _PersonaManager) -> SimpleNamespace:
    return SimpleNamespace(
        main_task_service=None,
        memory_manager=manager,
        _memory_runtime_settings=SimpleNamespace(assembly=SimpleNamespace(core_tools=[])),
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
    )


def _builder(manager: _PersonaManager) -> CeoMessageBuilder:
    return CeoMessageBuilder(loop=_loop(manager), prompt_builder=_PromptBuilder())


async def _build(builder: CeoMessageBuilder, session: SimpleNamespace):
    return await builder.build_for_ceo(
        session=session,
        query_text="ordinary question",
        exposure={"tool_names": [], "tools": [], "skills": []},
        persisted_session=None,
        user_content="ordinary question",
    )


@pytest.mark.asyncio
async def test_persona_block_follows_memory_and_stays_frozen_until_adopted() -> None:
    manager = _PersonaManager(document="261026:prefer concise answers\n", persona="你是玲音。")
    builder = _builder(manager)
    session = _session()

    first = await _build(builder, session)
    stable = list(first.stable_messages or [])
    assert [item["role"] for item in stable[:3]] == ["system", "assistant", "assistant"]
    assert "261026:prefer concise answers" in str(stable[1]["content"] or "")
    assert str(stable[2]["content"] or "").startswith("## 我的人设\n")
    assert "我是谁：你是玲音。" in str(stable[2]["content"] or "")

    manager.persona = "你是另一个人。"
    second = await _build(builder, session)
    assert str(second.stable_messages[2]["content"]) == str(stable[2]["content"])

    adopt_persona(session, memory_manager=manager, reason="token_compression")
    third = await _build(builder, session)
    assert "我是谁：你是另一个人。" in str(third.stable_messages[2]["content"] or "")


@pytest.mark.asyncio
async def test_persona_block_renders_the_unknown_identity_and_photo_folder() -> None:
    manager = _PersonaManager(document="", persona="")
    result = await _build(_builder(manager), _session())
    persona_rows = [
        item
        for item in list(result.stable_messages or [])
        if str(item.get("content") or "").startswith("## 我的人设\n")
    ]
    assert len(persona_rows) == 1
    content = str(persona_rows[0]["content"] or "")
    assert "## 我的人设" in content
    assert "my_image" in content
    assert "我是谁：未知（在 Negi中运行）" in content
    assert "未设置（默认是 Negi）" not in content


def test_persona_notice_for_missing_persona_asks_the_user() -> None:
    notice = render_persona_notice("")
    assert "目前人设不存在，需要向用户询问自己的人设" in notice
    assert "Respond as Negi" not in notice


def test_continuation_strips_and_reinserts_exactly_one_persona_block() -> None:
    manager = _PersonaManager(document="new memory", persona="新的人设")
    builder = _builder(manager)
    seed = [
        {"role": "system", "content": "BASE PROMPT"},
        {"role": "assistant", "content": "## 长期记忆\nold memory"},
        {"role": "assistant", "content": "## 我的人设\n- 我是谁：旧的人设"},
        {"role": "user", "content": "hello"},
    ]
    model_messages, stable, _appendix, _overlay, _in_history = builder._inject_direct_request_body_continuation(
        request_body_seed_messages=seed,
        user_content="next",
        turn_overlay_parts=[],
        memory_snapshot_text="new memory",
        persona_text="新的人设",
        query_text="next",
        user_metadata=None,
        pinned_contract_text="",
    )
    persona_rows = [item for item in stable if str(item.get("content") or "").startswith("## 我的人设\n")]
    assert len(persona_rows) == 1
    assert "我是谁：新的人设" in str(persona_rows[0]["content"])
    assert str(stable[1]["content"]).startswith("## 长期记忆\n")
    assert str(stable[2]["content"]).startswith("## 我的人设\n")


@pytest.mark.asyncio
async def test_persona_notice_aligns_then_delivers_once_and_rebases_after_compression() -> None:
    clear_skill_roster_notice_stores()
    scope = "session:web:persona-notice"
    manager = _PersonaManager(persona="v1")
    ops = object.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(memory_manager=manager)

    def provider(*, adopted: str):
        return lambda **kwargs: ops._frontdoor_persona_notice_provider(
            **kwargs,
            adopted_digest=persona_digest(adopted),
        )

    first, _ = deliver_for_scope(
        scope,
        [],
        now="2026-10-26T00:00:00+08:00",
        persona_provider=provider(adopted="v1"),
    )
    assert first is None
    assert notice_context_for(scope)["persona_cursor"] == {
        "digest": persona_digest("v1"),
        "initialized": True,
    }

    manager.persona = "v2"
    second, _ = deliver_for_scope(
        scope,
        [],
        now="2026-10-26T00:01:00+08:00",
        persona_provider=provider(adopted="v1"),
    )
    content = str(second.get("content") or "")
    assert "### Persona updates" in content
    assert "v2" in content
    assert notice_context_for(scope)["persona_cursor"]["digest"] == persona_digest("v2")

    third, _ = deliver_for_scope(
        scope,
        [],
        now="2026-10-26T00:02:00+08:00",
        persona_provider=provider(adopted="v1"),
    )
    assert third is None

    # Compression reparints the head and aligns the cursor to the newly adopted digest.
    reset_persona_cursor(scope)
    fourth, _ = deliver_for_scope(
        scope,
        [],
        now="2026-10-26T00:03:00+08:00",
        persona_provider=provider(adopted="v2"),
    )
    assert fourth is None


def test_persona_change_between_explicit_adoption_and_first_notice_is_delivered() -> None:
    clear_skill_roster_notice_stores()
    scope = "session:web:persona-adopt-change"
    manager = _PersonaManager(persona="adopted value")
    ops = object.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(memory_manager=manager)

    manager.persona = "changed before first notice"
    message, _ = deliver_for_scope(
        scope,
        [],
        now="2026-10-26T00:00:00+08:00",
        persona_provider=lambda **kwargs: ops._frontdoor_persona_notice_provider(
            **kwargs,
            adopted_digest=persona_digest("adopted value"),
        ),
    )
    assert message is not None
    assert "changed before first notice" in str(message.get("content") or "")


def test_notice_provider_compares_against_the_session_adopted_digest() -> None:
    clear_skill_roster_notice_stores()
    manager = _PersonaManager(persona="v1")
    ops = object.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(memory_manager=manager)
    ops._frontdoor_contract_notice_sections = lambda *_args, **_kwargs: {}
    messages = [{"role": "user", "content": "hello"}]

    unchanged = ops._apply_frontdoor_skill_roster_notice(
        messages,
        session_key="web:wiring",
        state={"session_key": "web:wiring"},
        persona_adopted_digest=persona_digest("v1"),
    )
    assert unchanged == messages

    manager.persona = "v2"
    changed = ops._apply_frontdoor_skill_roster_notice(
        messages,
        session_key="web:wiring",
        state={"session_key": "web:wiring"},
        persona_adopted_digest=persona_digest("v1"),
    )
    notices = [
        item for item in changed if str(item.get("content") or "").startswith("[RUNTIME_NOTICE]")
    ]
    assert len(notices) == 1
    assert "v2" in str(notices[0]["content"])


def test_frontdoor_prompt_requires_identity_and_persona_photo_handling() -> None:
    from pathlib import Path

    prompt = (Path(__file__).resolve().parents[2] / "g3ku/runtime/prompts/ceo_frontdoor.md").read_text(
        encoding="utf-8"
    )
    assert "头部「我的人设」" in prompt
    assert "没有给出时，你的身份未知" in prompt
    assert "询问用户你是谁" in prompt
    assert "没有给出时，你就是 Negi" not in prompt
    assert "大体相同的，直接合并成一段描述" in prompt
    assert "存在无法同时成立的冲突时" in prompt
    assert "用户发来的是你（主 Agent）的照片或形象时" in prompt
    assert "filesystem_copy" in prompt
    assert "memory_update" in prompt
    assert "my_image" in prompt
