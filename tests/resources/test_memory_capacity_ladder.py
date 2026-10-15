from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _markdown_memory():
    return importlib.import_module("g3ku.agent.markdown_memory")


def _cfg(*, trigger: int = 16000, target: int = 13000, document_max: int = 20000, summary_max: int = 300):
    schema = importlib.import_module("g3ku.config.schema")
    cfg = schema.MemoryToolsConfig()
    cfg.document.compress_trigger_chars = trigger
    cfg.document.compress_target_chars = target
    cfg.document.document_max_chars = document_max
    cfg.document.summary_max_chars = summary_max
    return cfg


def _manager(tmp_path: Path, **cfg_kwargs):
    return _module().MemoryManager(tmp_path, _cfg(**cfg_kwargs))


def _add(manager, *, body: str, minimal: str, now_iso: str = "2026-04-25T00:00:00+08:00", source: str = "self") -> str:
    row = manager._memory_repo.create_memory(
        memory_body=body,
        minimal_memory=minimal,
        source=source,
        from_user=False,
        now_iso=now_iso,
    )
    return str(row["memory_id"])


def _set_compressed(manager, memory_ids: list[str]) -> None:
    for memory_id in memory_ids:
        manager._memory_repo.mark_compressed(memory_id)


def _paint(manager) -> None:
    manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:30+08:00")


def _rows(manager) -> dict[str, dict]:
    return {str(row["memory_id"]): row for row in manager._memory_repo.list_memories()}


def _app_config(tmp_path: Path) -> object:
    """A config whose `memory` role resolves, so `run_due_batch_once` reaches the model call."""
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
                "roleIterations": {"ceo": 40, "execution": 16, "inspection": 16, "memory": 6},
                "roleConcurrency": {"ceo": None, "execution": None, "inspection": None, "memory": 1},
                "multiAgent": {"orchestratorModelKey": None},
            },
            "models": {
                "catalog": [
                    {
                        "key": "memory-primary",
                        "providerModel": "openai:gpt-4.1",
                        "apiKey": "demo-key",
                        "apiBase": None,
                        "extraHeaders": None,
                        "enabled": True,
                        "maxTokens": 1,
                        "temperature": 0.1,
                        "reasoningEffort": "low",
                        "retryOn": [],
                        "description": "",
                    }
                ],
                "roles": {
                    "ceo": ["memory-primary"],
                    "execution": ["memory-primary"],
                    "inspection": ["memory-primary"],
                    "memory": ["memory-primary"],
                },
            },
            "providers": {"openai": {"apiKey": "", "apiBase": None, "extraHeaders": None}},
        }
    )


def _survival_order(manager, inspired: list[str]) -> None:
    """Push every row except `inspired` down the survival order, the way a real turn does."""
    manager._memory_repo.apply_mutation(
        adds=None,
        rewrites=None,
        deletes=None,
        inspired_memory_ids=inspired,
        now_iso="2026-04-25T00:00:31+08:00",
    )


def _pool_ids(manager, **kwargs) -> list[str]:
    return [str(row.get("memory_id") or "") for row, _gain in manager._compression_pool_rows(**kwargs)]


def test_over_trigger_compresses_and_never_queues_a_consolidation(tmp_path: Path) -> None:
    """Rung one has to be enough whenever the pool can pay for it."""
    manager = _manager(tmp_path, trigger=400, target=300)
    try:
        for index in range(4):
            _add(manager, body=f"第{index}条" + "内" * 88, minimal=f"条件{index}->关键词", now_iso=f"2026-04-25T00:00:0{index}+08:00")
        _paint(manager)
        assert manager._resident_memory_chars() > 400

        result = manager._compress_memory_snapshot_if_needed(now_iso="2026-04-25T00:00:40+08:00")

        assert result["compression_triggered"] is True
        assert result["capacity_action"] == "compressed"
        assert result["consolidation_requested"] is False
        assert manager._queued_consolidation_request() is None, "no model round is spent while the machine still has room"
        assert manager._resident_memory_chars() <= 400
        assert all(int(item["freed_chars"]) > 0 for item in result["compressed_details"])
    finally:
        manager.close()


def test_the_protected_tail_and_this_batches_own_rows_are_never_downgraded(tmp_path: Path) -> None:
    """The guard is a per-pass byte budget, and it excludes the rows being written now."""
    manager = _manager(tmp_path, trigger=400, target=300)
    try:
        ids = [
            _add(manager, body=f"第{index}条" + "内" * 88, minimal=f"条件{index}->关键词", now_iso=f"2026-04-25T00:00:0{index}+08:00")
            for index in range(4)
        ]
        _paint(manager)
        # The head of the survival order is what may be sacrificed; the tail is the bytes the
        # model keeps using, and no pass may take more than 7/10 of what can still shrink.
        _survival_order(manager, inspired=[ids[-1]])

        pool = _pool_ids(manager)
        assert ids[-1] not in pool, "the most-referenced row sits past the budget"
        assert pool, "the head of the order is still fair game"

        result = manager._compress_memory_snapshot_if_needed(now_iso="2026-04-25T00:00:40+08:00")
        marked = set(result["compressed_memory_ids"])
        assert ids[-1] not in marked
        assert marked <= set(pool)

        excluded = _pool_ids(manager, exclude_ids=[ids[0]])
        assert ids[0] not in excluded, "a row this very batch wrote is exempt for this round"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_a_batch_too_big_for_the_library_is_held_behind_a_consolidation_then_parked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Rung two asks for room; rung three parks the payload verbatim after two replays."""
    module = _module()
    manager = _manager(tmp_path, trigger=400, target=300, document_max=900)
    try:
        for index in range(4):
            _add(manager, body=f"第{index}条" + "内" * 88, minimal=f"条件{index}->关键词", now_iso=f"2026-04-25T00:00:0{index}+08:00")
        _set_compressed(manager, list(_rows(manager)))
        _paint(manager)
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="把这条长期规则记住",
                created_at="2026-04-25T00:00:32+08:00",
                request_id="write_1",
            )
        )

        async def _respond(*, batch=None, **_kwargs):
            if str(getattr(batch, "op", "") or "").lower() == "consolidate":
                # The consolidator runs clean and declines: nothing here fits a category.
                return {
                    "validated": module._MemoryValidatedWrite(
                        document_text="",
                        note_writes={},
                        note_refs_written=[],
                        memory_chars_after=0,
                        document_preview="",
                        is_consolidation=True,
                        noop_reason="这些记忆都不属于任何分类",
                    ),
                    "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
                    "attempt_count": 1,
                    "assessed_text": None,
                    "model_chain": ["memory-primary"],
                    "provider_request_ids": [],
                    "request_artifact_paths": [],
                }
            return {
                "validated": None,
                "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
                "attempt_count": 3,
                "assessed_text": None,
                "capacity_replay": True,
                "error": "memory document exceeds 900 chars",
                "model_chain": ["memory-primary"],
                "provider_request_ids": [],
                "request_artifact_paths": [],
            }

        monkeypatch.setattr(manager, "_run_memory_agent_batch", _respond)
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path), 1, False),
            raising=False,
        )

        first = await manager.run_due_batch_once(now_iso="2026-04-25T00:00:40+08:00")
        assert first["status"] == "capacity_replay"
        assert first["consolidation_queued"] is True
        queue_rows = manager._read_queue_requests()
        assert str(queue_rows[0].op) == "consolidate", "the consolidation goes ahead of the batch it must free room for"
        held = next(row for row in queue_rows if row.request_id == "write_1")
        assert held.status == "pending"
        assert held.capacity_replay_count == 1

        # The next tick takes the head: the consolidation runs and declines, which parks it
        # so the same candidate set cannot be queued again on every later commit.
        second = await manager.run_due_batch_once(now_iso="2026-04-25T00:01:40+08:00")
        assert second["status"] == "applied"
        assert second["op"] == "consolidate"
        noop_rows = [
            json.loads(line)
            for line in (tmp_path / "memory" / "failed.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("category") == "capacity_noop"
        ]
        assert noop_rows, "a declined consolidation is a durable fact, not a silent no-op"
        gated_payload, gated_candidates = manager._build_consolidation_payload(trigger=400, target=300)
        assert gated_candidates, "the same rows are still what a consolidation would be offered"
        assert manager._noop_consolidation_blocks(gated_candidates) is True, "and the gate refuses to re-queue them"

        third = await manager.run_due_batch_once(now_iso="2026-04-25T00:02:40+08:00")
        assert third["status"] == "capacity_replay"
        assert third["capacity_replay_count"] == 2
        assert third["consolidation_queued"] is False, "the gate holds; the queue does not grow another consolidation"
        assert third["consolidation_reason"] == "noop_parked"

        fourth = await manager.run_due_batch_once(now_iso="2026-04-25T00:03:40+08:00")
        assert fourth["status"] == "parked"
        assert fourth["discard_reason"] == "capacity_exhausted"
        assert fourth["capacity_action"] == "parked"
        assert all(row.request_id != "write_1" for row in manager._read_queue_requests())
        failed = json.loads((tmp_path / "memory" / "failed.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert failed["category"] == "capacity"
        assert "把这条长期规则记住" in json.dumps(failed["items"], ensure_ascii=False), "held, never rewritten"
        ops_rows = [json.loads(line) for line in (tmp_path / "memory" / "ops.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        assert any(row.get("status") == "parked" and row.get("discard_reason") == "capacity_exhausted" for row in ops_rows)
    finally:
        manager.close()


def test_a_capacity_refusal_frees_room_and_the_payload_is_priced_again(tmp_path: Path) -> None:
    """Rung one is a machine move: the model's staged text is untouched and no repair round is spent."""
    module = _module()
    manager = _manager(tmp_path, trigger=300, target=250, document_max=600)
    try:
        for index in range(4):
            _add(manager, body=f"第{index}条" + "内" * 88, minimal=f"条件{index}->关键词", now_iso=f"2026-04-25T00:00:0{index}+08:00")
        _paint(manager)
        before_rows = _rows(manager)
        resident_before = manager._resident_memory_chars()
        assert resident_before > 300

        session = module._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
        )
        staged_body = "新规则->必须整条装下" + "外" * 120
        session.apply_batch(adds=[{"content": staged_body, "minimal_memory": "新规则->短", "decision_source": "user"}])
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="user",
                    payload_text="新规则",
                    created_at="2026-04-25T00:00:32+08:00",
                    request_id="write_1",
                )
            ],
        )
        # Without the lane this candidate is over the cap: the refusal is what rung one fixes.
        assert resident_before + manager._row_rendered_chars(
            {"memory_id": "Nw0001", "memory_body": staged_body, "minimal_memory": "新规则->短", "source": "user"}
        ) > 600

        validated = manager._validate_candidate_state(batch=batch, before_text=manager.snapshot_text(), session=session)

        assert validated.capacity_action == "compressed"
        assert validated.compressed_details, "the machine made room before asking the model to shrink"
        assert set(before_rows) <= set(_rows(manager)), "compressing never removes a row"
        assert sum(int(item["freed_chars"]) for item in validated.compressed_details) > 0
    finally:
        manager.close()


def test_consolidation_candidates_are_only_rows_the_machine_already_downgraded(tmp_path: Path) -> None:
    """A memory leaves the block only after it was judged worth downgrading twice over."""
    manager = _manager(tmp_path, trigger=120, target=100)
    try:
        compressed_one = _add(manager, body="已压" + "内" * 60, minimal="已压->关键词", now_iso="2026-04-25T00:00:01+08:00")
        raw_one = _add(manager, body="未压" + "内" * 60, minimal="未压->关键词", now_iso="2026-04-25T00:00:02+08:00")
        _set_compressed(manager, [compressed_one])
        _paint(manager)

        candidate_ids = [str(row["memory_id"]) for row in manager._consolidation_candidate_rows()]

        assert candidate_ids == [compressed_one], "an untouched row is not eligible for retirement"
        assert raw_one not in candidate_ids
        _payload, picked = manager._build_consolidation_payload(trigger=120, target=100)
        assert picked == [compressed_one]
        assert "已压" + "内" * 60 in _payload, "only the candidate list changes, not the text handed over"
    finally:
        manager.close()


def test_consolidation_prices_rows_by_what_they_cost_the_block_not_by_their_bodies(tmp_path: Path) -> None:
    """A downgraded row occupies its summary line, so that is the only honest ruler."""
    manager = _manager(tmp_path, trigger=1200, target=1000)
    try:
        fat_ids = [
            _add(
                manager,
                body="长正文" + "内" * 250,
                minimal="条件->词",
                now_iso=f"2026-04-25T00:00:{index:02d}+08:00",
            )
            for index in range(1, 25)
        ]
        _set_compressed(manager, fat_ids)
        # Two untouched rows carry the block over the trigger; retiring only downgraded rows
        # has to make up the whole gap out of their summary lines.
        _add(manager, body="新规则" + "外" * 250, minimal="新的->词", now_iso="2026-04-25T00:01:21+08:00")
        _add(manager, body="另一条" + "外" * 250, minimal="另一->词", now_iso="2026-04-25T00:01:22+08:00")
        _paint(manager)
        resident = manager._resident_memory_chars()
        assert resident > 1200
        render_each = manager._row_rendered_chars(_rows(manager)[fat_ids[0]])
        assert render_each < 60, "a downgraded row renders to its summary, not its 800-char body"

        _payload, picked = manager._build_consolidation_payload(trigger=1200, target=1000)

        # Priced by bodies, one row would look like enough for a gap of ~400 chars; priced
        # honestly the payload has to reach past the gap or retirement leaves the block over.
        assert len(picked) > 1
        assert sum(manager._row_rendered_chars(_rows(manager)[memory_id]) for memory_id in picked) >= resident - 1000
        assert set(picked) <= set(fat_ids), "the two rows nobody downgraded are not on the list"
    finally:
        manager.close()


def test_a_row_that_cannot_shrink_still_gets_the_consolidation_door(tmp_path: Path) -> None:
    """Marking costs nothing on these rows, and skipping them would strand them forever."""
    manager = _manager(tmp_path, trigger=150, target=120)
    try:
        same = "同字" + "内" * 60
        flat = _add(manager, body=same, minimal=same, now_iso="2026-04-25T00:00:01+08:00")
        shrink = _add(manager, body="可压" + "内" * 60, minimal="可压->词", now_iso="2026-04-25T00:00:02+08:00")
        _paint(manager)
        assert manager._resident_memory_chars() > 150

        result = manager._compress_memory_snapshot_if_needed(now_iso="2026-04-25T00:00:40+08:00")

        marked = {str(item["memory_id"]): int(item["freed_chars"]) for item in result["compressed_details"]}
        assert flat in marked and marked[flat] == 0, "downgraded for the door it opens, not for bytes"
        assert _rows(manager)[flat]["is_compressed"] is True
        assert flat in [str(row["memory_id"]) for row in manager._consolidation_candidate_rows()]
        assert manager._resident_memory_chars() >= 1, "and the block did not grow"
        assert shrink in marked
    finally:
        manager.close()


def test_a_summary_longer_than_its_body_is_never_downgraded(tmp_path: Path) -> None:
    """Compressing such a row would lengthen the block, so a loop stopping at target never stops."""
    manager = _manager(tmp_path, trigger=200, target=100)
    try:
        grown = _add(manager, body="短" * 10, minimal="条件" + "->" + "关" * 60, now_iso="2026-04-25T00:00:01+08:00")
        normal = _add(manager, body="正" * 90, minimal="条件->词", now_iso="2026-04-25T00:00:02+08:00")
        _paint(manager)
        before = manager._resident_memory_chars()

        compressed = manager._compress_pool_to_target(now_iso="2026-04-25T00:00:40+08:00", target=100)

        assert [str(item["memory_id"]) for item in compressed] == [normal]
        assert _rows(manager)[grown]["is_compressed"] is False
        assert manager._resident_memory_chars() < before, "the lane only ever moves the number down"
    finally:
        manager.close()


@pytest.mark.parametrize("shape", ["add", "rewrite"])
def test_a_summary_may_not_be_longer_than_the_memory_it_sums_up(tmp_path: Path, shape: str) -> None:
    """Both write points are gated, and the rewrite is priced against its own new body."""
    module = _module()
    manager = _manager(tmp_path)
    try:
        existing = _add(manager, body="原有正文" + "内" * 40, minimal="原有->关键词", now_iso="2026-04-25T00:00:01+08:00")
        _paint(manager)

        session = module._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
        )
        long_minimal = "条件->" + "关" * 40
        if shape == "add":
            session.apply_batch(adds=[{"content": "短正文", "minimal_memory": long_minimal, "decision_source": "user"}])
        else:
            session.apply_batch(rewrites=[{"id": existing, "content": "短正文", "minimal_memory": long_minimal}])
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="user",
                    payload_text="改一下",
                    created_at="2026-04-25T00:00:32+08:00",
                    request_id="write_1",
                )
            ],
        )

        with pytest.raises(module._MemoryAgentValidationError) as refused:
            manager._validate_candidate_state(batch=batch, before_text=manager.snapshot_text(), session=session)

        message = str(refused.value)
        assert "最小记忆不能比正文长" in message
        assert str(len(long_minimal)) in message and str(len("短正文")) in message
        assert "note" in message, "and it says where the detail goes instead"
    finally:
        manager.close()


def test_an_equal_summary_is_still_accepted(tmp_path: Path) -> None:
    """60 of the live 90 rows have `minimal == body`; the cap is `<=`, not `<`."""
    module = _module()
    manager = _manager(tmp_path)
    try:
        text = "日报投递->QQ桥纯文本不分段"
        session = module._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
        )
        session.apply_batch(adds=[{"content": text, "minimal_memory": text, "decision_source": "user"}])
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="user",
                    payload_text="记一条",
                    created_at="2026-04-25T00:00:32+08:00",
                    request_id="write_1",
                )
            ],
        )

        validated = manager._validate_candidate_state(batch=batch, before_text=manager.snapshot_text(), session=session)

        assert len(validated.adds) == 1
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_manual_warming_restores_the_full_body_and_rolls_back_at_the_cap(tmp_path: Path) -> None:
    """回暖是梯子唯一的手动挡：它把字符加回常驻块，所以它也要过硬顶这道校验。"""
    manager = _manager(tmp_path, trigger=300, target=250, document_max=480)
    try:
        keep = _add(manager, body="留着" + "内" * 200, minimal="留着->词", now_iso="2026-04-25T00:00:01+08:00")
        warm = _add(manager, body="回暖这条" + "外" * 240, minimal="回暖->词", now_iso="2026-04-25T00:00:02+08:00")
        _set_compressed(manager, [keep, warm])
        _paint(manager)
        assert _rows(manager)[warm]["is_compressed"] is True

        result = await manager.uncompress_current_memory(warm)

        assert result["changed"] is True
        assert _rows(manager)[warm]["is_compressed"] is False
        assert _rows(manager)[warm]["memory_body"] == "回暖这条" + "外" * 240
        assert _rows(manager)[keep]["is_compressed"] is True, "warming one row never touches another"
        assert "回暖这条" in manager.snapshot_text()

        # Warming the second row now pushes the document past the cap. The row has to go back
        # to its downgraded form: a store ahead of its mirror is the state the ladder reads.
        with pytest.raises(Exception):
            await manager.uncompress_current_memory(keep)
        rows_after = _rows(manager)
        assert rows_after[keep]["is_compressed"] is True, "a rejected warming rolls back whole"
        assert rows_after[warm]["is_compressed"] is False, "and the rollback is to this batch's start, not to zero"
        assert "回暖这条" in manager.snapshot_text()
        assert ("留着" + "内" * 200) not in manager.snapshot_text()
        assert result["resident_chars_after"] <= 480
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_warming_a_row_that_is_not_downgraded_changes_nothing(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        plain = _add(manager, body="全文常驻" + "内" * 20, minimal="全文->词")
        _paint(manager)

        result = await manager.uncompress_current_memory(plain)

        assert result["changed"] is False
        assert _rows(manager)[plain]["is_compressed"] is False
    finally:
        manager.close()


def test_the_warming_lane_is_operator_only(tmp_path: Path) -> None:
    """Nothing on the machine side may clear `is_compressed`, or the ladder oscillates."""
    manager = _manager(tmp_path)
    try:
        source = Path(importlib.import_module("g3ku.agent.memory_agent_runtime").__file__ or "").read_text(
            encoding="utf-8"
        )
        callers = [
            line.strip()
            for line in source.splitlines()
            if "mark_uncompressed(" in line and "def mark_uncompressed" not in line
        ]
        assert len(callers) == 1, "the only caller is the operator lane"
        assert "repo.mark_uncompressed(target_id)" in callers[0]
    finally:
        manager.close()


def test_the_capacity_rung_is_recorded_per_memory_in_the_ledger(tmp_path: Path) -> None:
    """`capacity_action` names the rung and every downgraded row carries what it gave back."""
    module = _module()
    manager = _manager(tmp_path, trigger=300, target=200)
    try:
        ids = [
            _add(manager, body=f"第{index}条" + "内" * 88, minimal=f"条件{index}->词", now_iso=f"2026-04-25T00:00:0{index}+08:00")
            for index in range(3)
        ]
        _paint(manager)
        write = module._MemoryValidatedWrite(
            document_text="",
            note_writes={},
            note_refs_written=[],
            memory_chars_after=0,
            document_preview="",
            adds=[{"id": "Nw0001", "content": "新的一条" + "外" * 20, "minimal_memory": "新的->词", "decision_source": "user"}],
        )
        manager._commit_validated_write(write)

        assert write.capacity_action == "compressed"
        assert write.compressed_details
        changes = manager._build_structured_changes(write)
        compressions = [item for item in changes if item.get("type") == "compress_for_capacity"]
        assert compressions and all(int(item["freed_chars"]) >= 0 for item in compressions)
        assert {str(item["memory_id"]) for item in compressions} <= set(ids)
    finally:
        manager.close()
