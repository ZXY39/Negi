"""Stage 3：入参暂存（stash）的容器规则与两条耐久载体。

覆盖 `docs/FIX_PLAN_tool_hydration_contract_presence.md` §4 Stage 3 的五条验证，加上两条
"东西真的活过路上每一层"的核对：

- 同轮同工具 3 次**不同**入参 ⇒ 列表长度 3、顺序保持（不是 last-write-wins）；
- 同签名的重复调用按同一口径折叠（digest 只看归一化后的内容，键序无关）；
- 严格一跳：第 N 跳落、第 N+1 跳可取，第 N+2 跳为空，`sweep_expired` 清掉；
- 阶段切换 / 契约离场 ⇒ 按工具名摘掉；
- pause/resume 与重启后仍可读 ⇒ 节点走真 SQLite（`node.metadata` 整份 `model_dump_json`
  落库，无键过滤），前门走真连续性快照白名单（不登记就静默不落盘，这是这条道唯一的坑）。
"""

from __future__ import annotations

from pathlib import Path

from g3ku.runtime.tool_arg_stash import (
    ARGS_FIELD,
    STASH_CONTAINER_KEY,
    args_digest,
    consume_entries,
    drop_scope,
    drop_staged_after,
    node_scope_key,
    normalize_tool_arg_stash,
    peek_entries,
    read_container,
    session_scope_key,
    stage_args,
    staged_count,
    staged_targets,
    sweep_expired,
    write_container,
)
from g3ku.runtime.web_ceo_sessions import _normalized_completed_continuity_snapshot
from main.models import NodeRecord
from main.storage.sqlite_store import SQLiteTaskStore

SCOPE = node_scope_key("task-1", "node-1", "filesystem_edit")
SESSION_SCOPE = session_scope_key("web:shared", "filesystem_edit")
FINGERPRINT = "tcf:aaaaaaaaaaaaaaaa"


def _stage(container: dict, args: dict, *, hop: int, staged_at: str = "2026-10-20T20:00:00+08:00", scope: str = SCOPE) -> dict:
    next_container, _entry, _count = stage_args(
        container,
        scope_key=scope,
        args=args,
        fingerprint=FINGERPRINT,
        staged_at=staged_at,
        staged_hop=hop,
    )
    return next_container


# ---------------------------------------------------------------------------
# 同轮多次：全保、按序
# ---------------------------------------------------------------------------


def test_same_round_multiple_calls_are_all_kept_in_order() -> None:
    container: dict = {}
    for index, path in enumerate(["/abs/a", "/abs/b", "/abs/c"]):
        container = _stage(container, {"path": path, "new_text": f"v{index}"}, hop=5)
    assert staged_count(container, scope_key=SCOPE) == 3
    entries = peek_entries(container, scope_key=SCOPE, current_hop=6, fingerprint=FINGERPRINT)
    assert [entry[ARGS_FIELD]["path"] for entry in entries] == ["/abs/a", "/abs/b", "/abs/c"]


def test_digest_is_key_order_insensitive_so_same_signature_collapses() -> None:
    assert args_digest({"a": "1", "b": "2"}) == args_digest({"b": "2", "a": "1"})
    assert args_digest({"a": "1"}) != args_digest({"a": "2"})


def test_consume_takes_every_staged_set_and_leaves_none() -> None:
    container = _stage(_stage({}, {"path": "/abs/a"}, hop=5), {"path": "/abs/b"}, hop=5)
    next_container, consumed = consume_entries(container, scope_key=SCOPE, current_hop=6, fingerprint=FINGERPRINT)
    assert [entry[ARGS_FIELD]["path"] for entry in consumed] == ["/abs/a", "/abs/b"]
    assert next_container == {}


# ---------------------------------------------------------------------------
# 严格一跳
# ---------------------------------------------------------------------------


def test_strict_one_hop_window() -> None:
    container = _stage({}, {"path": "/abs/a"}, hop=5)
    assert len(peek_entries(container, scope_key=SCOPE, current_hop=6)) == 1
    # 第 N+2 跳：模型上一跳没调这个工具，暂存过期。
    assert peek_entries(container, scope_key=SCOPE, current_hop=7) == []
    swept, dropped = sweep_expired(container, current_hop=7)
    assert swept == {}
    assert dropped == [SCOPE]


def test_revision_mismatch_is_not_consumable() -> None:
    """契约换了修订 ⇒ 老暂存不能复用（D7 前置：执行的是当前修订的契约）。"""
    container = _stage({}, {"path": "/abs/a"}, hop=5)
    assert peek_entries(container, scope_key=SCOPE, current_hop=6, fingerprint="tcf:other") == []
    assert len(peek_entries(container, scope_key=SCOPE, current_hop=6, fingerprint=FINGERPRINT)) == 1


def test_contract_absent_sweep_drops_the_scope() -> None:
    container = _stage({}, {"path": "/abs/a"}, hop=5)
    swept, dropped = sweep_expired(container, current_hop=6, present_tool_ids={"content_open"})
    assert swept == {}
    assert dropped == [SCOPE]
    # 没有在场名单可读时不清（撤销是收紧，读不到判据不该顺手删状态）。
    kept, untouched = sweep_expired(container, current_hop=6, present_tool_ids=None)
    assert untouched == []
    assert staged_count(kept, scope_key=SCOPE) == 1


# ---------------------------------------------------------------------------
# 阶段切换 / 终态 / 历史回截
# ---------------------------------------------------------------------------


def test_stage_switch_drops_by_tool_id() -> None:
    container = _stage({}, {"path": "/abs/a"}, hop=5)
    container = _stage(container, {"url": "https://example.com"}, hop=5, scope=session_scope_key("web:shared", "web_fetch"))
    cleared = drop_scope(container, tool_id="filesystem_edit")
    assert staged_count(cleared, scope_key=SCOPE) == 0
    assert staged_count(cleared, scope_key=session_scope_key("web:shared", "web_fetch")) == 1


def test_history_rewind_drops_entries_staged_after_the_cutoff() -> None:
    early = _stage({}, {"path": "/keep"}, hop=1, staged_at="2026-10-20T10:00:00+08:00")
    both = _stage(early, {"path": "/drop"}, hop=2, staged_at="2026-10-20T18:00:00+08:00")
    rewound = drop_staged_after(both, cutoff_iso="2026-10-20T12:00:00+08:00")
    targets = staged_targets(rewound, scope_key=SCOPE)
    assert [entry[ARGS_FIELD]["path"] for entry in targets] == ["/keep"]
    # 缺时间戳的条目按最保守方向丢。
    assert drop_staged_after({"scope_like:x": [{ARGS_FIELD: {}}]}, cutoff_iso="2026-10-20T12:00:00+08:00") == {}


# ---------------------------------------------------------------------------
# 载体：节点 metadata 真落 SQLite
# ---------------------------------------------------------------------------


def test_node_metadata_carrier_survives_a_store_round_trip(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    container = _stage({}, {"path": "/abs/a", "new_text": "x"}, hop=5)
    node = NodeRecord(
        node_id="node-1",
        task_id="task-1",
        root_node_id="node-1",
        goal="g",
        prompt="p",
        created_at="2026-10-20T20:00:00+08:00",
        updated_at="2026-10-20T20:00:00+08:00",
        metadata={"execution_stages": {"stages": []}, **write_container({}, container)},
    )
    store.upsert_node(node)

    # 换一个新 store 句柄：等价于进程重启 / pause 后 resume 再读。
    reloaded = SQLiteTaskStore(tmp_path / "runtime.sqlite3").get_node("node-1")
    assert reloaded is not None
    restored = read_container(reloaded.metadata)
    assert [entry[ARGS_FIELD]["path"] for entry in peek_entries(restored, scope_key=SCOPE, current_hop=6)] == ["/abs/a"]
    # 邻居键不受影响（写 stash 不能顺手抹掉阶段账本）。
    assert "execution_stages" in reloaded.metadata


def test_write_container_prunes_empty_scopes() -> None:
    carrier = write_container({"other": 1}, {SCOPE: []})
    assert STASH_CONTAINER_KEY not in carrier
    assert carrier["other"] == 1


# ---------------------------------------------------------------------------
# 载体：前门连续性快照的白名单
# ---------------------------------------------------------------------------


def test_continuity_snapshot_keeps_the_stash_key() -> None:
    container = _stage({}, {"path": "/abs/a"}, hop=5, scope=SESSION_SCOPE)
    snapshot = _normalized_completed_continuity_snapshot(
        {
            "session_key": "web:shared",
            "updated_at": "2026-10-20T20:00:00+08:00",
            STASH_CONTAINER_KEY: container,
        }
    )
    assert STASH_CONTAINER_KEY in snapshot
    restored = read_container(snapshot)
    assert [entry[ARGS_FIELD]["path"] for entry in restored[SESSION_SCOPE]] == ["/abs/a"]


def test_continuity_snapshot_drops_malformed_entries_instead_of_storing_them() -> None:
    snapshot = _normalized_completed_continuity_snapshot(
        {
            "session_key": "web:shared",
            STASH_CONTAINER_KEY: {
                "bogus_shape": [{"args": {"a": 1}}],
                SCOPE: ["not-a-dict", {"args": {"path": "/ok"}}],
            },
        }
    )
    restored = read_container(snapshot)
    assert list(restored) == [SCOPE]
    assert restored[SCOPE][0][ARGS_FIELD] == {"path": "/ok"}
    # 归一化会补 digest（缺字段时按内容算），执行侧才有的可对账。
    assert restored[SCOPE][0]["args_digest"] == args_digest({"path": "/ok"})


def test_normalize_accepts_bare_container_and_wrapped_forms() -> None:
    wrapped = normalize_tool_arg_stash({STASH_CONTAINER_KEY: {SCOPE: [{"args": {"a": 1}, "staged_hop": 3}]}})
    bare = normalize_tool_arg_stash({SCOPE: [{"args": {"a": 1}, "staged_hop": 3}]})
    assert wrapped == bare
    assert normalize_tool_arg_stash(None) == {}
    assert normalize_tool_arg_stash({"tool_arg_stash": "not-a-dict"}) == {}
