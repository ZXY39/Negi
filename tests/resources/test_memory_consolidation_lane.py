from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _cfg(*, trigger: int = 16000, target: int = 13000, document_max: int = 20000):
    schema = importlib.import_module("g3ku.config.schema")
    cfg = schema.MemoryToolsConfig()
    cfg.document.compress_trigger_chars = trigger
    cfg.document.compress_target_chars = target
    cfg.document.document_max_chars = document_max
    return cfg


def _manager(tmp_path: Path, **cfg_kwargs):
    return _module().MemoryManager(tmp_path, _cfg(**cfg_kwargs))


def _add(
    manager,
    *,
    body: str,
    minimal: str,
    source: str = "self",
    from_user: bool = False,
    note_ref: str = "",
    now_iso: str = "2026-04-25T00:00:00+08:00",
) -> str:
    row = manager._memory_repo.create_memory(
        memory_body=body,
        minimal_memory=minimal,
        source=source,
        from_user=from_user,
        now_iso=now_iso,
        note_ref=note_ref,
    )
    return str(row["memory_id"])


def _retire(manager, memory_ids: list[str], category: str) -> None:
    manager._memory_repo.mark_consolidated(memory_ids, category_ref=category, now_iso="2026-04-25T00:00:01+08:00")


def _write_category(manager, category: str, body: str) -> None:
    path = manager.index_dir / f"{category}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _markdown_memory():
    return importlib.import_module("g3ku.agent.markdown_memory")


def test_retiring_writes_the_column_and_never_the_delete_button(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    markdown_memory = _markdown_memory()
    try:
        memory_id = _add(manager, body="反代端口改成 8799 之后要重启 nginx", minimal="反代->端口")
        _write_category(manager, "note_local_env", "本机运行环境\n\n## 反代\n端口 8799，改完重启 nginx。\n")

        manager._commit_validated_write(
            module._MemoryValidatedWrite(
                document_text="",
                note_writes={},
                note_refs_written=[],
                memory_chars_after=0,
                document_preview="",
                is_consolidation=True,
                consolidation_writes=[
                    {"note_ref": "note_local_env", "body": "本机运行环境\n\n## 反代\n端口 8799，改完重启 nginx。\n", "edits": []}
                ],
                retirements=[{"memory_id": memory_id, "note_ref": "note_local_env"}],
            )
        )

        rows = {row["memory_id"]: row for row in manager._memory_repo.list_memories()}
        assert memory_id in rows, "retiring must not delete the row"
        assert rows[memory_id]["consolidated_into"] == "note_local_env"
        assert rows[memory_id]["memory_body"] == "反代端口改成 8799 之后要重启 nginx"

        document = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
        entries_text, block_text = markdown_memory.split_consolidation_block(document)
        assert "反代端口改成" not in entries_text
        assert block_text.startswith(markdown_memory.CONSOLIDATION_HEADING)
        assert "本机运行环境" in block_text
        assert f"(note: {markdown_memory.note_handle('note_local_env', category=True)})" in block_text
        assert "note_local_env" not in block_text, "the resident line shows the handle, not the name"
    finally:
        manager.close()


def test_the_reminder_line_only_appears_when_the_block_has_rows(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    markdown_memory = _markdown_memory()
    try:
        _add(manager, body="普通一条记忆", minimal="普通->一条")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:02+08:00")
        document = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
        assert markdown_memory.CONSOLIDATION_HEADING not in document

        memory_id = _add(manager, body="第二条要退场的记忆", minimal="第二条->退场")
        _write_category(manager, "note_tools", "工具技巧\n\n## curl\n带中文的 body 要落文件。\n")
        _retire(manager, [memory_id], "note_tools")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:03+08:00")

        document = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
        assert markdown_memory.CONSOLIDATION_REMINDER in document
    finally:
        manager.close()


def test_a_category_emptied_out_later_loses_its_index_line(tmp_path: Path) -> None:
    """Index rows are derived from the column, not enumerated from the directory: an empty
    category must stop occupying the resident block by itself."""
    manager = _manager(tmp_path)
    markdown_memory = _markdown_memory()
    try:
        memory_id = _add(manager, body="要退场的记忆", minimal="退场->记忆")
        _write_category(manager, "note_ops", "运维\n\n## 端口\n18790\n")
        _retire(manager, [memory_id], "note_ops")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:04+08:00")
        assert "运维" in (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")

        manager._memory_repo.delete_memory(memory_id)
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:05+08:00")

        document = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
        assert "运维" not in document
        assert markdown_memory.CONSOLIDATION_HEADING not in document
    finally:
        manager.close()


def test_a_broken_category_link_drops_the_line_and_is_reported(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, body="指向不存在分类的记忆", minimal="断链->记忆")
        _retire(manager, [memory_id], "note_gone")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:06+08:00")

        assert "note_gone" not in (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")

        report = manager.doctor_report(now_iso="2026-04-25T00:00:07+08:00")
        assert report["broken_category_links"] == ["note_gone"]
        assert report["ok"] is False
        assert report["consolidated_categories"] == 0
    finally:
        manager.close()


def test_the_admin_browser_lists_every_class_with_its_count(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        # 分类按行首次出现排序，行的读序是 (created_at, memory_id)：时间必须给开，
        # 同一时间戳下 memory_id 是随机串，顺序就成了跑一次变一次。
        first = _add(
            manager,
            body="反代端口改成 8799 之后要重启 nginx",
            minimal="本机环境->端口",
            now_iso="2026-04-25T00:00:01+08:00",
        )
        second = _add(
            manager,
            body="网页端口固定 18790，占用时不许自动换",
            minimal="本机环境->网页端口",
            now_iso="2026-04-25T00:00:02+08:00",
        )
        _write_category(
            manager,
            "note_local_env",
            "要改本机端口或重启反代时打开\n\n## 反代端口\n8799\n",
        )
        _retire(manager, [first, second], "note_local_env")
        broken = _add(
            manager,
            body="指向不存在分类的记忆",
            minimal="断链->记忆",
            now_iso="2026-04-25T00:00:03+08:00",
        )
        _retire(manager, [broken], "note_gone")

        items = manager.list_consolidation_categories()
        assert [str(item["note_ref"]) for item in items] == ["note_local_env", "note_gone"]
        assert items[0]["scope_line"] == "要改本机端口或重启反代时打开"
        assert items[0]["entry_count"] == 2
        assert items[0]["unusable"] is False
        assert items[0]["handle"] == _markdown_memory().note_handle("note_local_env", category=True)
        # 常驻行不渲染的断链分类，浏览面仍要列出来：否则操作员看到的是"少了一类"而不是"链断了"。
        assert items[1]["unusable"] is True
        assert items[1]["scope_line"] == ""
        assert items[1]["entry_count"] == 1

        rows, broken_refs = manager._consolidation_categories()
        assert [ref for _line, ref in rows] == ["note_local_env"]
        assert broken_refs == ["note_gone"]
    finally:
        manager.close()


def test_the_consolidation_block_is_excluded_from_both_counting_judgements(tmp_path: Path) -> None:
    """Permanent bytes must not feed a trigger that measures retireable bytes: with the
    block counted, every later commit would queue another consolidation forever. The cap
    is excluded for the same reason, so a wide merge area can always be written."""
    module = _module()
    manager = _manager(tmp_path, trigger=120, target=100, document_max=130)
    try:
        keep = _add(manager, body="K" * 20, minimal="保留->短")
        retire = _add(manager, body="R" * 20, minimal="退场->短")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:08+08:00")

        manager._commit_validated_write(
            module._MemoryValidatedWrite(
                document_text="",
                note_writes={},
                note_refs_written=["note_big"],
                memory_chars_after=0,
                document_preview="",
                is_consolidation=True,
                consolidation_writes=[{"note_ref": "note_big", "body": "大类\n\n" + "S" * 3000 + "\n", "edits": []}],
                retirements=[{"memory_id": retire, "note_ref": "note_big"}],
            )
        )

        markdown_memory = _markdown_memory()
        document = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
        assert markdown_memory.CONSOLIDATION_HEADING in document
        assert f"- 大类 (note: {markdown_memory.note_handle('note_big', category=True)})" in document
        assert "(note: note_big)" not in document
        assert "S" * 3000 not in document, "only the index line is resident; the body loads on demand"
        assert len(document) > 130, "the document exceeds document_max and still landed"
        assert manager._resident_memory_chars() <= 120

        result = manager._compress_memory_snapshot_if_needed(now_iso="2026-04-25T00:00:09+08:00")
        assert result["consolidation_requested"] is False
        assert [row.op for row in manager._read_queue_requests()] == []
        assert keep in {row["memory_id"] for row in manager._memory_repo.list_memories()}
    finally:
        manager.close()


def test_consolidation_staging_refuses_both_directions_of_orphaned_work(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, body="要退场的记忆", minimal="退场->记忆")

        retired_without_home = module._ConsolidationToolSession(
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
        )
        result = retired_without_home.apply_batch(
            retirements=[{"memory_id": memory_id, "note_ref": "note_nowhere"}]
        )
        assert result["ok"] is False
        assert "note_nowhere" in json.dumps(result["errors"], ensure_ascii=False)

        written_without_retirement = module._ConsolidationToolSession(
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
        )
        result = written_without_retirement.apply_batch(
            note_writes=[{"note_ref": "note_empty_category", "body": "空分类\n"}]
        )
        assert result["ok"] is False
        assert "note_empty_category" in json.dumps(result["errors"], ensure_ascii=False)

        noop = module._ConsolidationToolSession(
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
        )
        assert noop.apply_batch(no_consolidation_reason="交来的三条都不属于任何分类")["ok"] is True
        mixed = module._ConsolidationToolSession(
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
        )
        assert mixed.apply_batch(
            note_writes=[{"note_ref": "note_x", "body": "x\n"}],
            retirements=[{"memory_id": memory_id, "note_ref": "note_x"}],
            no_consolidation_reason="同时又说没有",
        )["ok"] is False

        unknown = module._ConsolidationToolSession(
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
        )
        unknown.apply_batch(
            note_writes=[{"note_ref": "note_x", "body": "x\n"}],
            retirements=[{"memory_id": "ZZZZZZ", "note_ref": "note_x"}],
        )
        with pytest.raises(module._MemoryAgentValidationError, match="retire ids not found"):
            manager._build_consolidation_write_from_apply_batch(
                batch=module.MemoryBatch(op="consolidate", items=[]),
                session=unknown,
            )
    finally:
        manager.close()


def test_category_notes_are_reachable_from_the_ceo_reader_and_the_ordinary_agent(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        _write_category(manager, "note_resume", "简历类\n\n## 排版\n一页 A4。\n")

        assert "一页 A4" in manager.load_note("note_resume")

        session = module._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="write",
        )
        assert "一页 A4" in session.read_note("note_resume")

        prompt = manager._memory_agent_user_prompt(
            batch=module.MemoryBatch(op="write", items=[]),
            snapshot_text=manager.snapshot_text(),
            repair_reason="",
        )
        assert "note_resume" in prompt
    finally:
        manager.close()


def test_an_ordinary_note_owned_by_a_retired_memory_survives_the_sweep(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(
            manager,
            body="细节写在 note 里的一条记忆",
            minimal="细节->见正文",
            note_ref="note_deep",
        )
        note_path = manager.notes_dir / "note_deep.md"
        note_path.write_text("细节正文\n", encoding="utf-8")
        manager._memory_repo.apply_mutation(
            adds=[],
            rewrites=[],
            deletes=[],
            inspired_memory_ids=None,
            now_iso="2026-04-25T00:00:10+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:11+08:00")

        _write_category(manager, "note_tools", "工具技巧\n\n## 备注\n略\n")
        _retire(manager, [memory_id], "note_tools")

        removed = manager._cleanup_orphan_notes_locked()

        assert removed == []
        assert note_path.exists()
    finally:
        manager.close()


def test_the_injected_snapshot_carries_the_block_verbatim(tmp_path: Path) -> None:
    """The display projection strips ids and headers from entries; the block has to pass
    through untouched, and nothing may re-read `memory/index` while injecting."""
    builder_module = importlib.import_module("g3ku.runtime.frontdoor.message_builder")
    markdown_memory = _markdown_memory()

    entries = (
        "---\nid:Ab12Z9\n2026/4/17-user：\n用户偏好：回复先给结论\n"
        f"\n{markdown_memory.CONSOLIDATION_HEADING}\n{markdown_memory.CONSOLIDATION_REMINDER}\n"
        "- 工具技巧 (note: note_tools)\n"
    )

    message = builder_module.CeoMessageBuilder._memory_snapshot_stable_message(entries)

    content = str(message["content"])
    assert content.startswith("## 长期记忆")
    assert "id:Ab12Z9" not in content
    assert "2026/4/17-user：" not in content
    assert "用户偏好：回复先给结论" in content
    assert markdown_memory.CONSOLIDATION_REMINDER in content
    assert "- 工具技巧 (note: note_tools)" in content


def test_a_consolidation_that_retires_nothing_still_moves_the_trigger_forward(tmp_path: Path) -> None:
    """No category fit these memories. Leaving the snapshot over the line would queue
    another consolidation on every later commit forever, so the machine degrades the
    oldest rows - and only that."""
    module = _module()
    manager = _manager(tmp_path, trigger=120, target=100, document_max=20000)
    try:
        first = _add(manager, body="F" * 90, minimal="第一条->短")
        second = _add(manager, body="S" * 90, minimal="第二条->短")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:22+08:00")

        manager._commit_validated_write(
            module._MemoryValidatedWrite(
                document_text="",
                note_writes={},
                note_refs_written=[],
                memory_chars_after=0,
                document_preview="",
                is_consolidation=True,
                noop_reason="两条都不属于任何分类",
            )
        )

        rows = {row["memory_id"]: row for row in manager._memory_repo.list_memories()}
        assert set(rows) == {first, second}, "the fallback never deletes"
        assert rows[first]["is_compressed"] is True
        assert manager._resident_memory_chars() <= 120
        assert manager._compress_memory_snapshot_if_needed(now_iso="2026-04-25T00:00:23+08:00")[
            "consolidation_requested"
        ] is False
    finally:
        manager.close()


def _app_config(tmp_path: Path) -> object:
    schema = importlib.import_module("g3ku.config.schema")
    return schema.Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path),
                    "runtime": "langgraph",
                    "maxTokens": 1,
                    "temperature": 0.1,
                    "maxToolIterations": 1,
                    "memoryWindow": 1,
                    "reasoningEffort": "low",
                },
                "roleIterations": {"memory": 6},
                "roleConcurrency": {"memory": 1},
            },
            "models": {
                "catalog": [
                    {
                        "key": "memory-primary",
                        "providerModel": "openai:gpt-4.1",
                        "apiKey": "demo-key",
                        "enabled": True,
                        "maxTokens": 1,
                        "temperature": 0.1,
                        "reasoningEffort": "low",
                        "retryOn": [],
                        "description": "",
                    }
                ],
                "roles": {"memory": ["memory-primary"]},
            },
            "providers": {"openai": {"apiKey": "", "apiBase": None, "extraHeaders": None}},
        }
    )


def _fake_response(*, content: str = "", tool_calls: list[dict[str, object]] | None = None, usage=None) -> object:
    usage_payload = dict(usage or {})
    return SimpleNamespace(
        content=content,
        tool_calls=list(tool_calls or []),
        usage_metadata=usage_payload,
        response_metadata={"token_usage": usage_payload},
    )


class _FakeToolCallingModel:
    def __init__(self, responses: list[object]):
        self._responses = list(responses)

    def bind_tools(self, tools):
        _ = tools
        return self

    async def chat(self, messages, tools=None, **kwargs):
        _ = tools, kwargs, messages
        if not self._responses:
            raise AssertionError("unexpected extra consolidation agent call")
        next_item = self._responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


async def test_a_consolidate_batch_runs_through_the_consolidator_lane(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, body="网页端口是 18790", minimal="网页->端口")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:12+08:00")
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="consolidate",
                decision_source="self",
                payload_text=f"## 待归并记忆\n- id:{memory_id}\n网页端口是 18790\n",
                created_at="2026-04-25T00:00:13+08:00",
                request_id="consolidate_1",
            )
        )

        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_consolidation_apply",
                            "args": {
                                "note_writes": [
                                    {
                                        "note_ref": "note_local_env",
                                        "body": "本机运行环境\n\n## 端口\n网页 18790，反代 8799。\n",
                                    }
                                ],
                                "retirements": [{"memory_id": memory_id, "note_ref": "note_local_env"}],
                            },
                        }
                    ],
                    usage={"input_tokens": 5, "output_tokens": 2, "cache_read_tokens": 0},
                ),
                _fake_response(content="done", usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}),
            ]
        )
        app_config = _app_config(tmp_path)
        monkeypatch.setattr(
            module, "get_runtime_config", lambda force=False: (app_config, 1, False), raising=False
        )
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: fake_model, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-25T00:00:20+08:00")

        assert report["status"] == "applied"
        rows = {row["memory_id"]: row for row in manager._memory_repo.list_memories()}
        assert rows[memory_id]["consolidated_into"] == "note_local_env"
        assert (manager.index_dir / "note_local_env.md").exists()

        ledger = [
            json.loads(line)
            for line in (tmp_path / "memory" / "ops.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        row = ledger[-1]
        assert row["category_count"] == 1
        assert [change["type"] for change in row["changes"]] == ["category_note", "consolidate"]
        assert row["changes"][1]["original_content"] == "网页端口是 18790"
    finally:
        manager.close()


def test_reconcile_notes_never_invents_a_category_note(tmp_path: Path) -> None:
    """A retired row pointing at a deleted category note is a report, not a file to
    create: a placeholder would put a resident line describing nothing into the block.
    The ordinary note a retired row owns still must not read as an orphan."""
    manager = _manager(tmp_path)
    try:
        ordinary = _add(manager, body="细节在 note 里", minimal="细节->正文", note_ref="note_deep")
        _write_category(manager, "note_details", "细节正文")
        _retire(manager, [ordinary], "note_details")
        category_id = _add(manager, body="分类被删掉的退场记忆", minimal="断链->分类")
        _retire(manager, [category_id], "note_missing_cat")

        report = manager.reconcile_notes(delete_orphans=True)

        assert not (manager.notes_dir / "note_missing_cat.md").exists()
        assert report["broken_category_links"] == ["note_missing_cat"]
        assert report["deleted_orphan_count"] == 0
        assert (manager.notes_dir / "note_deep.md").exists(), ordinary
    finally:
        manager.close()


def test_a_batch_may_only_retire_the_memories_it_was_offered(tmp_path: Path) -> None:
    """The consolidator sees the whole resident block for context. Without a scope check
    an eager batch can retire rows nobody handed it - the live probe did exactly that to
    a seventh row - so the candidate list in the batch payload is the boundary."""
    module = _module()
    manager = _manager(tmp_path)
    try:
        offered = _add(manager, body="被交来的那条要归并的记忆", minimal="交来->归并")
        untouched = _add(manager, body="没被交来的记忆", minimal="没交来->留下")

        def _session() -> object:
            session = module._ConsolidationToolSession(
                notes_dir=manager.notes_dir,
                index_dir=manager.index_dir,
            )
            session.apply_batch(
                note_writes=[{"note_ref": "note_env", "body": "本机运行环境 端口 18790", "edits": []}],
                retirements=[
                    {"memory_id": offered, "note_ref": "note_env"},
                    {"memory_id": untouched, "note_ref": "note_env"},
                ],
            )
            return session

        def _batch(payload_ids: list[str]) -> object:
            nl = chr(10)
            body = nl.join(f"- id:{memory_id}{nl}正文{nl}" for memory_id in payload_ids)
            return module.MemoryBatch(
                op="consolidate",
                items=[
                    module.MemoryQueueRequest(
                        op="consolidate",
                        decision_source="self",
                        payload_text=(
                            f"## 待归并记忆{nl}{body}{nl}## 现有分类 note{nl}-（还没有分类 note）{nl}"
                        ),
                        created_at="2026-04-25T00:00:30+08:00",
                        request_id="consolidate_scope_1",
                    )
                ],
            )

        with pytest.raises(module._MemoryAgentValidationError, match="not offered to this consolidation batch"):
            manager._build_consolidation_write_from_apply_batch(
                batch=_batch([offered]),
                session=_session(),
            )

        validated = manager._build_consolidation_write_from_apply_batch(
            batch=_batch([offered, untouched]),
            session=_session(),
        )
        assert len(validated.retirements) == 2

        prompt = manager._consolidation_agent_user_prompt(
            batch=_batch([offered]),
            snapshot_text=manager.snapshot_text(),
            repair_reason="",
        )
        assert "只能是上面「待归并记忆」列出的那些" in prompt
    finally:
        manager.close()


def test_a_category_note_may_not_reuse_an_ordinary_note_name(tmp_path: Path) -> None:
    """两份同名文件共用一个名字，读侧只能命中其中一个：归并侧在暂存层就拒。"""
    module = _module()
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, body="反代端口要写进笔记", minimal="端口->笔记", note_ref="note_ports")
        (manager.notes_dir / "note_ports.md").write_text("普通笔记正文\n", encoding="utf-8")

        session = module._ConsolidationToolSession(
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
        )
        result = session.apply_batch(
            note_writes=[{"note_ref": "note_ports", "body": "端口分类\n"}],
            retirements=[{"memory_id": memory_id, "note_ref": "note_ports"}],
        )

        assert result["ok"] is False
        assert "already an ordinary note" in json.dumps(result["errors"], ensure_ascii=False)
        assert not (manager.index_dir / "note_ports.md").exists()
    finally:
        manager.close()


def test_an_ordinary_note_may_not_reuse_a_category_note_name(tmp_path: Path) -> None:
    """反方向同一条规则：普通笔记不能占用已被分类 note 使用的名字。"""
    module = _module()
    manager = _manager(tmp_path)
    try:
        _write_category(manager, "note_deploy", "部署分类\n")

        session = module._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
        )
        result = session.apply_batch(note_upserts={"note_deploy": "部署笔记\n"})

        assert result["ok"] is False
        assert "already a category note" in json.dumps(result["errors"], ensure_ascii=False)
        assert not (manager.notes_dir / "note_deploy.md").exists()
    finally:
        manager.close()


def test_the_two_note_trees_keep_separate_handles(tmp_path: Path) -> None:
    markdown = _markdown_memory()
    manager = _manager(tmp_path)
    try:
        owner = _add(manager, body="端口细节写在普通笔记里", minimal="端口->普通笔记", note_ref="note_ports")
        (manager.notes_dir / "note_ports.md").write_text("普通笔记正文\n", encoding="utf-8")
        _write_category(manager, "note_ports", "分类笔记正文")
        _retire(manager, [owner], "note_ports")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:06+08:00")

        ordinary_handle = markdown.note_handle("note_ports")
        category_handle = markdown.note_handle("note_ports", category=True)
        assert ordinary_handle.startswith("n")
        assert category_handle.startswith("c")
        assert ordinary_handle != category_handle

        # 归并区那行带的是 c 句柄，读回来必须是 index/ 那份，不是同名普通笔记
        assert f"(note: {category_handle})" in manager._consolidation_block_text()
        assert manager.load_note(category_handle) == "分类笔记正文"
        assert manager.load_note(ordinary_handle) == "普通笔记正文\n"
        assert manager.resolve_note_ref(category_handle) == "note_ports"

        # 库里已经存在的同名冲突不再静默：doctor 报出来
        report = manager.doctor_report(now_iso="2026-04-25T00:00:07+08:00")
        assert report["note_name_collisions"] == ["note_ports"]
        assert "note_name_collisions" in [item["name"] for item in report["checks"] if not item["ok"]]
    finally:
        manager.close()


def test_reconcile_refuses_to_create_an_ordinary_note_over_a_category_name(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        owner = _add(manager, body="细节在笔记里", minimal="细节->笔记", note_ref="note_deep")
        _write_category(manager, "note_deep", "分类正文")
        _retire(manager, [owner], "note_deep")

        report = manager.reconcile_notes()

        # 只查 notes/ 时这个名字算"缺文件"，但造一份出去就坐实了同名两份
        assert report["skipped_name_collisions"] == ["note_deep"]
        assert "note_deep" not in report["created_missing_notes"]
        assert not (manager.notes_dir / "note_deep.md").exists()
    finally:
        manager.close()
