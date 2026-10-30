"""记忆分域：本地 / 群聊 / 第三方私聊。

不变量：
- 本地会话读写本地 manager；群会话读写本群 manager；第三方私聊只读本地。
- 群会话模型可见块 = 本地记忆 + 「## 群聊长期记忆」。
- 群 manager 不碰人设 note；群写入不落本地 manager。
- 群记忆增量走独立游标与独立小节，压缩跳同时归零。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import g3ku.runtime.external_sessions as external_sessions
from g3ku.config.schema import MemoryToolsConfig
from g3ku.runtime.memory_scope_router import GROUP_MEMORY_DIRNAME, MemoryScopeRouter
from g3ku.runtime.skill_roster_notice import (
    GROUP_MEMORY_NOTICE_HEADING,
    clear_skill_roster_notice_stores,
    deliver_for_scope,
    notice_context_for,
    reset_memory_cursor,
)


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _manager(workspace):
    return _module().MemoryManager(workspace, MemoryToolsConfig())


class _FakeRegistry:
    def __init__(self, entries):
        self._entries = {entry.session_key: entry for entry in entries}

    def get_by_session_key(self, session_key):
        return self._entries.get(str(session_key or "").strip())

    def list_entries(self):
        return list(self._entries.values())


def _group_entry(session_key="ext:qq-official-1:group-a", external_key="group-a", title="测试群"):
    return SimpleNamespace(
        bridge_id="qq-official-1",
        external_key=external_key,
        session_key=session_key,
        title=title,
        chat_type="group",
        trust="third_party_group",
    )


def _dm_entry(session_key="ext:qq-official-1:dm-a"):
    return SimpleNamespace(
        bridge_id="qq-official-1",
        external_key="dm-a",
        session_key=session_key,
        title="私聊",
        chat_type="dm",
        trust="third_party_dm",
    )


def _router(tmp_path, monkeypatch, entries=()):
    fake = _FakeRegistry(entries)
    monkeypatch.setattr(external_sessions, "get_external_session_registry", lambda: fake)
    local = _manager(tmp_path)
    router = MemoryScopeRouter(workspace=tmp_path, local_manager=local, config=MemoryToolsConfig())
    return router, local


def test_router_routes_local_group_and_readonly_dm(tmp_path, monkeypatch) -> None:
    router, local = _router(
        tmp_path,
        monkeypatch,
        entries=[_group_entry(), _dm_entry()],
    )
    try:
        assert router.view_for_session("web:ceo-1").kind == "local"
        group_view = router.view_for_session("ext:qq-official-1:group-a")
        assert group_view.kind == "group"
        assert group_view.review_manager is group_view.group_manager
        assert group_view.group_key
        dm_view = router.view_for_session("ext:qq-official-1:dm-a")
        assert dm_view.kind == "readonly_local"
        assert dm_view.review_manager is None
        assert dm_view.writable_manager is None
    finally:
        router.close()
        local.close()


def test_group_snapshot_is_local_plus_group_block(tmp_path, monkeypatch) -> None:
    router, local = _router(tmp_path, monkeypatch, entries=[_group_entry()])
    try:
        local.memory_file.write_text("本地规则：回答先给结论。\n", encoding="utf-8")
        view = router.view_for_session("ext:qq-official-1:group-a")
        view.group_manager.memory_file.write_text("群规则：这个群只看周报。\n", encoding="utf-8")
        text = view.snapshot_text()
        assert "本地规则" in text
        assert text.index("本地规则") < text.index("## 群聊长期记忆") < text.index("群规则")
        # 私聊看到的只有本地那一份
        dm = router.view_for_session("ext:qq-official-1:dm-a")
        assert dm.snapshot_text() == "本地规则：回答先给结论。"
    finally:
        router.close()
        local.close()


def test_group_manager_is_isolated_from_local_memory_and_persona(tmp_path, monkeypatch) -> None:
    router, local = _router(tmp_path, monkeypatch, entries=[_group_entry()])
    try:
        view = router.view_for_session("ext:qq-official-1:group-a")
        group_manager = view.group_manager
        assert group_manager.workspace != local.workspace
        assert group_manager.persona_enabled is False
        assert group_manager.persona_text() == ""
        with pytest.raises(ValueError):
            group_manager.set_persona("群聊专属人设")

        result = asyncio.run(
            group_manager.enqueue_write_request(
                session_key="ext:qq-official-1:group-a",
                decision_source="user",
                payload_text="记住这个群只讨论发布计划。",
                trigger_source="memory_update_tool",
            )
        )
        assert result["status"] == "queued"
        group_queue = (group_manager.workspace / "memory" / "queue.jsonl").read_text(encoding="utf-8")
        assert "发布计划" in group_queue
        local_queue = (local.workspace / "memory" / "queue.jsonl").read_text(encoding="utf-8")
        assert "发布计划" not in local_queue
    finally:
        router.close()
        local.close()


def test_group_note_read_falls_back_to_local(tmp_path, monkeypatch) -> None:
    router, local = _router(tmp_path, monkeypatch, entries=[_group_entry()])
    try:
        local.notes_dir.mkdir(parents=True, exist_ok=True)
        (local.notes_dir / "note_local1.md").write_text("本地笔记正文", encoding="utf-8")
        view = router.view_for_session("ext:qq-official-1:group-a")
        view.group_manager.notes_dir.mkdir(parents=True, exist_ok=True)
        (view.group_manager.notes_dir / "note_group1.md").write_text("群笔记正文", encoding="utf-8")

        assert "群笔记正文" in view.read_note("note_group1")
        assert "本地笔记正文" in view.read_note("note_local1")
    finally:
        router.close()
        local.close()


def test_router_lists_scopes_and_resolves_admin_scope(tmp_path, monkeypatch) -> None:
    router, local = _router(tmp_path, monkeypatch, entries=[_group_entry()])
    try:
        scopes = router.list_scopes()
        assert scopes[0]["kind"] == "local"
        group_scope = [item for item in scopes if item["kind"] == "group"]
        assert len(group_scope) == 1
        assert group_scope[0]["title"] == "测试群"
        key = group_scope[0]["scope"]
        assert router.manager_for_scope(key) is not None
        assert router.manager_for_scope("missing-scope") is None
        assert (tmp_path / "memory" / GROUP_MEMORY_DIRNAME / key).is_dir()
    finally:
        router.close()
        local.close()


def test_group_memory_notice_uses_its_own_heading_and_cursor() -> None:
    clear_skill_roster_notice_stores()
    try:
        message, _ = deliver_for_scope(
            "session:ext:qq-official-1:group-a",
            [],
            now="2026-10-30T10:00:00+08:00",
            group_memory_provider=lambda **_: {
                "rev": 1,
                "from_rev": 0,
                "text": "- 群规则：只看周报",
                "rebase": False,
            },
        )
        assert message is not None
        assert GROUP_MEMORY_NOTICE_HEADING in message["content"]
        assert "只看周报" in message["content"]
        assert notice_context_for("session:ext:qq-official-1:group-a")["group_memory_cursor"]["rev"] == 1

        reset_memory_cursor("session:ext:qq-official-1:group-a")
        cursor = notice_context_for("session:ext:qq-official-1:group-a")["group_memory_cursor"]
        assert cursor == {"rev": 0, "base_rev": 0, "initialized": False}
    finally:
        clear_skill_roster_notice_stores()


class _FakeWritableManager:
    def __init__(self):
        self.calls = []

    async def enqueue_write_request(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"ok": True, "status": "queued", "request_id": "req-1"}


def test_memory_update_tool_routes_to_the_group_manager() -> None:
    from g3ku.agent.tools.memory_update import MemoryUpdateTool

    local = _FakeWritableManager()
    group = _FakeWritableManager()
    view = SimpleNamespace(writable_manager=group, review_manager=group, kind="group")
    tool = MemoryUpdateTool(manager=local)
    payload = asyncio.run(
        tool.execute(
            content="记住这个群只讨论发布计划",
            __g3ku_runtime={"session_key": "ext:qq-official-1:group-a", "memory_view": view},
        )
    )
    assert json.loads(payload)["status"] == "queued"
    assert group.calls and not local.calls
    assert group.calls[0]["session_key"] == "ext:qq-official-1:group-a"


def test_memory_update_tool_refuses_readonly_third_party_dm() -> None:
    from g3ku.agent.tools.memory_update import MemoryUpdateTool

    local = _FakeWritableManager()
    view = SimpleNamespace(writable_manager=None, review_manager=None, kind="readonly_local")
    tool = MemoryUpdateTool(manager=local)
    payload = asyncio.run(
        tool.execute(
            content="记住我的生日",
            __g3ku_runtime={"session_key": "ext:qq-official-1:dm-a", "memory_view": view},
        )
    )
    body = json.loads(payload)
    assert body["ok"] is False
    assert body["reason"] == "memory_read_only"
    assert not local.calls


def test_memory_note_tool_reads_through_the_scope_view() -> None:
    from g3ku.agent.tools.memory_note import MemoryNoteTool

    class _Local:
        def load_note(self, ref):
            return "本地"

    class _View:
        def read_note(self, ref):
            return f"域内:{ref}"

    tool = MemoryNoteTool(manager=_Local())
    assert tool._manager.load_note("x") == "本地"
    assert asyncio.run(tool.execute(ref="note_a", __g3ku_runtime={"memory_view": _View()})) == "域内:note_a"


def test_admin_memory_scopes_endpoint_lists_local_and_groups(monkeypatch) -> None:
    from main.api import admin_rest

    fake_router = SimpleNamespace(
        list_scopes=lambda: [
            {"scope": "", "kind": "local", "title": "本地记忆", "session_key": ""},
            {"scope": "grp-1", "kind": "group", "title": "测试群", "session_key": "ext:x:1"},
        ]
    )
    monkeypatch.setattr(admin_rest, "get_agent", lambda: SimpleNamespace(memory_scope_router=fake_router))
    payload = asyncio.run(admin_rest.get_memory_scopes())
    assert [item["kind"] for item in payload["items"]] == ["local", "group"]
    assert payload["items"][1]["scope"] == "grp-1"


def test_config_bundle_round_trips_group_memory(tmp_path) -> None:
    from g3ku.config import config_bundle
    from g3ku.security import get_bootstrap_security_service

    source = tmp_path / "source"
    (source / ".g3ku").mkdir(parents=True)
    (source / ".g3ku" / "config.json").write_text(
        json.dumps({"resources": {"statePath": ".g3ku/resources.state.json"}}),
        encoding="utf-8",
    )
    local = _manager(source)
    group = _manager(source / "memory" / "groups" / "grp-1")
    try:
        local.memory_file.write_text("本地记忆\n", encoding="utf-8")
        group.memory_file.write_text("群记忆\n", encoding="utf-8")
        group.notes_dir.mkdir(parents=True, exist_ok=True)
        (group.notes_dir / "note_group1.md").write_text("群笔记\n", encoding="utf-8")
        service = get_bootstrap_security_service(source)
        service.setup_initial_realm(password="owner-password")
        archive = Path(
            config_bundle.export_bundle(
                source,
                password="bundle-password",
                use_project_password=False,
                scopes=("memory",),
            )["path"]
        )
    finally:
        group.close()
        local.close()

    target = tmp_path / "target"
    (target / ".g3ku").mkdir(parents=True)
    (target / ".g3ku" / "config.json").write_text("{}", encoding="utf-8")
    result = config_bundle.import_bundle(
        target,
        archive_path=archive,
        password="bundle-password",
        scopes=("memory",),
    )
    assert result["memory_entry_count"] >= 3
    group_root = target / "memory" / "groups" / "grp-1" / "memory"
    assert (group_root / "MEMORY.md").read_text(encoding="utf-8").strip() == "群记忆"
    assert (group_root / "notes" / "note_group1.md").read_text(encoding="utf-8").strip() == "群笔记"


LONG_GROUP_SCOPE = (
    "qq-official-1903529517_qq_group_E373126E86944E0642F5508C39AFEF9F-3a35c4f7"
)


def test_group_scope_name_accepts_generated_long_keys_and_rejects_unsafe() -> None:
    from g3ku.runtime.memory_scope_router import _group_key, group_scope_name_ok

    # 真实 QQ 群的作用域键 73 字符，远超表情包目录的 64 字符上限。
    assert len(LONG_GROUP_SCOPE) > 64
    assert group_scope_name_ok(LONG_GROUP_SCOPE)
    assert group_scope_name_ok(_group_key("qoder:p5-acceptance-group"))
    assert not group_scope_name_ok("")
    assert not group_scope_name_ok("..")
    assert not group_scope_name_ok(".hidden")
    assert not group_scope_name_ok("trailing.")
    assert not group_scope_name_ok(" padded")
    assert not group_scope_name_ok("bad:name")
    assert not group_scope_name_ok("x" * 129)
    assert not group_scope_name_ok("CON")


def test_group_scope_key_is_stable_across_devices_for_the_same_account_and_group() -> None:
    from g3ku.runtime.memory_scope_router import _group_key_for_entry

    same_account = SimpleNamespace(
        bridge_id="qq-official-1903529517",
        external_key="qq:group:48A6F16E277B61B66694A8C756371201",
        session_key="ext:other-device:local-transcript-hash",
    )
    original = SimpleNamespace(
        bridge_id="qq-official-1903529517",
        external_key="qq:group:48A6F16E277B61B66694A8C756371201",
        session_key="ext:qq-official-1903529517:first",
    )
    other_account = SimpleNamespace(
        bridge_id="qq-official-2000000000",
        external_key="qq:group:48A6F16E277B61B66694A8C756371201",
        session_key="ext:another-bot:first",
    )
    # 键只由 bridge_id + external_key 决定：同一账号同一群在任何设备上落到同一个目录。
    assert _group_key_for_entry(original, fallback="x") == _group_key_for_entry(same_account, fallback="y")
    # 不同桥账号是不同作用域，不因为群名相同就串记忆。
    assert _group_key_for_entry(original, fallback="x") != _group_key_for_entry(other_account, fallback="y")


def test_router_discovers_imported_group_scopes_without_a_registry_entry(tmp_path, monkeypatch) -> None:
    imported = tmp_path / "memory" / GROUP_MEMORY_DIRNAME / LONG_GROUP_SCOPE / "memory"
    imported.mkdir(parents=True)
    (imported / "MEMORY.md").write_text("导入的群记忆\n", encoding="utf-8")
    hidden = tmp_path / "memory" / GROUP_MEMORY_DIRNAME / ".hidden" / "memory"
    hidden.mkdir(parents=True)

    router, local = _router(tmp_path, monkeypatch, entries=[])
    try:
        scopes = router.list_scopes()
        discovered = [item for item in scopes if item["scope"] == LONG_GROUP_SCOPE]
        assert len(discovered) == 1
        assert discovered[0]["kind"] == "group"
        assert discovered[0]["session_key"] == ""
        assert all(item["scope"] != ".hidden" for item in scopes)
        manager = router.manager_for_scope(LONG_GROUP_SCOPE)
        assert manager is not None
        assert manager.snapshot_text().strip() == "导入的群记忆"
        assert router.manager_for_scope("../escape") is None
    finally:
        router.close()
        local.close()


def test_config_bundle_round_trips_a_long_group_scope_name(tmp_path) -> None:
    import sqlite3

    from g3ku.config import config_bundle
    from g3ku.security import get_bootstrap_security_service

    source = tmp_path / "source"
    (source / ".g3ku").mkdir(parents=True)
    (source / ".g3ku" / "config.json").write_text(
        json.dumps({"resources": {"statePath": ".g3ku/resources.state.json"}}),
        encoding="utf-8",
    )
    local_mem = source / "memory"
    local_mem.mkdir()
    (local_mem / "MEMORY.md").write_text("本地记忆\n", encoding="utf-8")
    with sqlite3.connect(local_mem / "memory_state.sqlite3") as conn:
        conn.execute("CREATE TABLE memories (memory_id TEXT PRIMARY KEY, memory_body TEXT)")
        conn.execute("INSERT INTO memories VALUES ('local-1', 'local body')")
    group_mem = local_mem / "groups" / LONG_GROUP_SCOPE / "memory"
    group_mem.mkdir(parents=True)
    (group_mem / "MEMORY.md").write_text("长名群记忆\n", encoding="utf-8")
    with sqlite3.connect(group_mem / "memory_state.sqlite3") as conn:
        conn.execute("CREATE TABLE memories (memory_id TEXT PRIMARY KEY, memory_body TEXT)")
        conn.execute("INSERT INTO memories VALUES ('group-1', 'long group body')")
    get_bootstrap_security_service(source).setup_initial_realm(password="owner-password")

    archive = Path(
        config_bundle.export_bundle(
            source,
            password="bundle-password",
            use_project_password=False,
            scopes=("memory",),
        )["path"]
    )

    target = tmp_path / "target"
    (target / ".g3ku").mkdir(parents=True)
    (target / ".g3ku" / "config.json").write_text("{}", encoding="utf-8")
    config_bundle.import_bundle(
        target,
        archive_path=archive,
        password="bundle-password",
        scopes=("memory",),
    )
    restored = target / "memory" / "groups" / LONG_GROUP_SCOPE / "memory"
    assert (restored / "MEMORY.md").read_text(encoding="utf-8").strip() == "长名群记忆"
    with sqlite3.connect(restored / "memory_state.sqlite3") as conn:
        assert conn.execute("SELECT memory_body FROM memories").fetchall() == [("long group body",)]


def test_memory_bundle_import_keeps_target_only_group_scopes(tmp_path) -> None:
    import sqlite3

    from g3ku.config import config_bundle
    from g3ku.security import get_bootstrap_security_service

    source = tmp_path / "source"
    (source / ".g3ku").mkdir(parents=True)
    (source / ".g3ku" / "config.json").write_text("{}", encoding="utf-8")
    local_mem = source / "memory"
    local_mem.mkdir()
    (local_mem / "MEMORY.md").write_text("本地记忆\n", encoding="utf-8")
    with sqlite3.connect(local_mem / "memory_state.sqlite3") as conn:
        conn.execute("CREATE TABLE memories (memory_id TEXT PRIMARY KEY)")
    get_bootstrap_security_service(source).setup_initial_realm(password="owner-password")

    target = tmp_path / "target"
    (target / ".g3ku").mkdir(parents=True)
    (target / ".g3ku" / "config.json").write_text("{}", encoding="utf-8")
    extra = target / "memory" / "groups" / "target-only-group" / "memory"
    extra.mkdir(parents=True)
    (extra / "MEMORY.md").write_text("目标机独有群记忆\n", encoding="utf-8")

    archive = Path(
        config_bundle.export_bundle(
            source,
            password="bundle-password",
            use_project_password=False,
            scopes=("memory",),
        )["path"]
    )
    config_bundle.import_bundle(
        target,
        archive_path=archive,
        password="bundle-password",
        scopes=("memory",),
    )
    # 导入只覆盖包内同名的群作用域；包外群目录保留（与本地记忆面同一语义）。
    assert (extra / "MEMORY.md").read_text(encoding="utf-8").strip() == "目标机独有群记忆"
