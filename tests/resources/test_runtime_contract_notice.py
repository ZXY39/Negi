"""运行时合同的一次性通知：头部快照覆盖静态项，变化只投一条，压缩后重置。"""

from __future__ import annotations

from types import SimpleNamespace

from g3ku.runtime import skill_roster_notice as notice
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor.tool_contract import (
    clear_pinned_contract_stores,
    frontdoor_pinned_contract_text,
    pinned_contract_section_values,
    skill_roster_epoch_for,
)
from main.runtime.react_loop import ReActToolLoop

SESSION_KEY = "web:contract-notice"
SCOPE = f"session:{SESSION_KEY}"
MARKER = notice.RUNTIME_NOTICE_MARKER

GOVERNED = {
    "mode": "governed",
    "guardrails_enabled": True,
    "summary": "exec will enforce exec-side guardrails.",
}
FULL_ACCESS = {
    "mode": "full_access",
    "guardrails_enabled": False,
    "summary": "exec will execute shell commands without exec-side guardrails.",
}


def _reset_stores() -> None:
    clear_pinned_contract_stores()
    notice.clear_skill_roster_notice_stores()


def _frontdoor_runner(policy_holder: dict[str, dict]):
    service = SimpleNamespace(
        list_contract_visible_skill_resources=lambda *, actor_role, session_id: [],
        list_effective_tool_names=lambda *, actor_role, session_id: ["other_tool"],
        _current_exec_runtime_policy_payload=lambda: dict(policy_holder["policy"]),
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=service)
    )
    runner._ceo_session_temp_dir = lambda _session_key: "C:/temp/ceo"
    return runner


def _pin_frontdoor_head(policy: dict) -> str:
    return frontdoor_pinned_contract_text(
        None,
        skill_items=[],
        exec_runtime_policy=policy,
        session_temp_dir="C:/temp/ceo",
        skill_roster_epoch=skill_roster_epoch_for(None, session_key=SESSION_KEY),
        session_key=SESSION_KEY,
    )


def _frontdoor_state() -> dict:
    return {
        "provider_tool_names": ["exec"],
        "candidate_tool_names": ["web_fetch"],
        "candidate_tool_items": [{"tool_id": "web_fetch", "description": "fetch"}],
        "repair_required_tool_items": [
            {"tool_id": "browser", "description": "browser", "reason": "missing deps"}
        ],
        "repair_required_skill_items": [],
        "attachment_reopen_targets": [
            {"name": "a.pdf", "kind": "file", "mime_type": "application/pdf", "path": "C:/a.pdf"}
        ],
    }


def _notice_texts(messages: list[dict]) -> list[str]:
    return [
        str(item.get("content") or "")
        for item in list(messages or [])
        if isinstance(item, dict) and str(item.get("content") or "").startswith(MARKER)
    ]


def test_frontdoor_head_covered_sections_are_not_reannounced() -> None:
    _reset_stores()
    holder = {"policy": dict(GOVERNED)}
    runner = _frontdoor_runner(holder)
    head = _pin_frontdoor_head(holder["policy"])

    first = runner._apply_frontdoor_skill_roster_notice(
        [{"role": "system", "content": head}, {"role": "user", "content": "hi"}],
        session_key=SESSION_KEY,
        state=_frontdoor_state(),
    )
    texts = _notice_texts(first)
    assert len(texts) == 1
    text = texts[0]
    # 头部快照已覆盖项不重复宣布
    assert "exec_runtime_policy" not in text
    assert "session_temp_dir" not in text
    # 非头部项按状态快照投一次
    assert "denied_tools: `exec`" in text
    assert "candidate_tools: `web_fetch`" in text
    assert "repair_required: `browser` (missing deps)" in text
    assert "attachment_targets: `a.pdf` → C:/a.pdf" in text

    # 同值不重复（旧通知还在传入历史里，判定"没有新增"）
    second = runner._apply_frontdoor_skill_roster_notice(
        first, session_key=SESSION_KEY, state=_frontdoor_state()
    )
    assert len(_notice_texts(second)) == len(texts)


def test_frontdoor_policy_change_is_one_notice_and_head_stays_frozen() -> None:
    _reset_stores()
    holder = {"policy": dict(GOVERNED)}
    runner = _frontdoor_runner(holder)
    head = _pin_frontdoor_head(holder["policy"])
    base = [{"role": "system", "content": head}, {"role": "user", "content": "hi"}]

    runner._apply_frontdoor_skill_roster_notice(base, session_key=SESSION_KEY, state=_frontdoor_state())

    holder["policy"] = dict(FULL_ACCESS)
    changed = runner._apply_frontdoor_skill_roster_notice(
        base, session_key=SESSION_KEY, state=_frontdoor_state()
    )
    texts = _notice_texts(changed)
    assert len(texts) == 1
    assert "exec_runtime_policy: mode=full_access" in texts[0]
    assert "guardrails_enabled=False" in texts[0]
    assert "denied_tools" not in texts[0]

    # 切策略不重印头部：重新取数仍是钉住的那一份原文。
    assert _pin_frontdoor_head(holder["policy"]) == head
    covered = pinned_contract_section_values(head)
    assert covered["exec_runtime_policy"].startswith("mode=governed")


def _node_loop() -> ReActToolLoop:
    class _LogService:
        def read_runtime_frame(self, task_id: str, node_id: str):
            return None

        def upsert_frame(self, task_id: str, frame: dict, publish_snapshot: bool = False) -> None:
            return None

    return ReActToolLoop(
        chat_backend=SimpleNamespace(),
        log_service=_LogService(),
        max_iterations=2,
    )


def test_node_notice_carries_only_non_head_sections() -> None:
    _reset_stores()
    loop = _node_loop()
    node = SimpleNamespace(
        task_id="task-notice",
        node_id="node-notice",
        depth=0,
        node_kind="execution",
        can_spawn_children=False,
        metadata={},
    )
    scope = "node:task-notice:node-notice"
    # 头部由主循环钉：这里先钉一份带 exec/临时目录的，模拟头部已覆盖。
    pinned = frontdoor_pinned_contract_text(
        None,
        skill_items=[],
        exec_runtime_policy=GOVERNED,
        session_temp_dir="C:/task/temp",
        skill_roster_epoch=0,
        session_key=scope,
    )
    covered = pinned_contract_section_values(pinned)

    sections = {
        # 与头部同一渲染口：值必须逐字一致，否则会被当成"策略变了"再投一条。
        "exec_runtime_policy": covered.get("exec_runtime_policy", ""),
        "session_temp_dir": covered.get("session_temp_dir", ""),
        "denied_tools": "`exec`",
        "candidate_tools": "`web_fetch`",
        "repair_required": "",
        "attachment_targets": "",
    }
    first = loop._apply_node_skill_roster_notice(
        [{"role": "system", "content": "node system"}, {"role": "user", "content": "{}"}],
        node=node,
        skill_roster_items=[],
        contract_sections=sections,
        covered_contract_sections=covered,
    )
    texts = _notice_texts(first)
    assert len(texts) == 1
    assert "exec_runtime_policy" not in texts[0]
    assert "session_temp_dir" not in texts[0]
    assert "denied_tools: `exec`" in texts[0]
    assert "candidate_tools: `web_fetch`" in texts[0]

    second = loop._apply_node_skill_roster_notice(
        first,
        node=node,
        skill_roster_items=[],
        contract_sections=sections,
        covered_contract_sections=covered,
    )
    assert len(_notice_texts(second)) == len(texts)


def test_frontdoor_compaction_strips_old_notice_and_replays_one_snapshot() -> None:
    """压缩后：旧通知剥离、台账清空；下一跳按新头部只投非头部状态快照。"""
    _reset_stores()
    holder = {"policy": dict(GOVERNED)}
    runner = _frontdoor_runner(holder)
    head = _pin_frontdoor_head(holder["policy"])
    base = [{"role": "system", "content": head}, {"role": "user", "content": "hi"}]

    first = runner._apply_frontdoor_skill_roster_notice(
        base, session_key=SESSION_KEY, state=_frontdoor_state()
    )
    assert len(_notice_texts(first)) == 1

    compacted = runner._reset_frontdoor_runtime_notice_after_compaction(
        first,
        session_key=SESSION_KEY,
        compression_stage_id="comp:1",
    )
    assert not any(str(item.get("content") or "").startswith(MARKER) for item in compacted)
    assert notice.notice_context_for(SCOPE)["contract_sections"] == {}

    state_after = _frontdoor_state()
    state_after["candidate_tool_items"] = []
    state_after["candidate_tool_names"] = []
    state_after["repair_required_tool_items"] = []
    state_after["attachment_reopen_targets"] = []
    state_after["provider_tool_names"] = ["exec"]
    replayed = runner._apply_frontdoor_skill_roster_notice(
        compacted, session_key=SESSION_KEY, state=state_after
    )
    texts = _notice_texts(replayed)
    assert len(texts) == 1
    # 头部覆盖项不再出现；仍存在的非头部状态重新快照一次
    assert "exec_runtime_policy" not in texts[0]
    assert "session_temp_dir" not in texts[0]
    assert "denied_tools: `exec`" in texts[0]
    assert "candidate_tools" not in texts[0]
    assert "repair_required" not in texts[0]
    assert "attachment_targets" not in texts[0]


def test_frontdoor_sticker_pack_change_is_one_notice_and_head_stays_frozen(monkeypatch) -> None:
    """表情包增删：头部冻结不动，只投一条 `sticker_pack:` 分节，同值不重复。"""
    from pathlib import Path

    from g3ku.stickers import Pack, StickerCategory

    _reset_stores()
    holder = {"policy": dict(GOVERNED)}
    runner = _frontdoor_runner(holder)

    atri = Pack(
        key="atri",
        theme="动漫少女",
        categories=(StickerCategory(key="happy", description="开心大笑", count=1),),
        path=Path("C:/ws/stickers/atri"),
    )
    panda = Pack(
        key="panda",
        theme="熊猫头",
        categories=(StickerCategory(key="smug", description="得意", count=1),),
        path=Path("C:/ws/stickers/panda"),
    )
    live = {"packs": (atri,)}
    monkeypatch.setattr(
        "g3ku.runtime.frontdoor._ceo_runtime_ops.safe_scan_packs", lambda: live["packs"]
    )

    head = frontdoor_pinned_contract_text(
        None,
        skill_items=[],
        exec_runtime_policy=holder["policy"],
        session_temp_dir="C:/temp/ceo",
        skill_roster_epoch=skill_roster_epoch_for(None, session_key=SESSION_KEY),
        session_key=SESSION_KEY,
        sticker_items=list(live["packs"]),
    )
    base = [{"role": "system", "content": head}, {"role": "user", "content": "hi"}]
    first = runner._apply_frontdoor_skill_roster_notice(
        base, session_key=SESSION_KEY, state=_frontdoor_state()
    )
    assert "sticker_pack:atri" not in "\n".join(_notice_texts(first))

    live["packs"] = (atri, panda)
    changed = runner._apply_frontdoor_skill_roster_notice(
        first, session_key=SESSION_KEY, state=_frontdoor_state()
    )
    assert "sticker_pack:panda: 熊猫头 | smug(得意)" in "\n".join(_notice_texts(changed))

    third = runner._apply_frontdoor_skill_roster_notice(
        changed, session_key=SESSION_KEY, state=_frontdoor_state()
    )
    assert len(_notice_texts(third)) == len(_notice_texts(changed))
