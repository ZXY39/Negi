"""节点详情底部「最新思考」那一行的正文车道。

节点账本每一轮都存着思考全文（`reasoning_content`），轨迹与摘要只发四个读数；
节点详情底部要显示最新一轮的思考过程，所以读侧（`detail_level='full'`）单独把
最近一轮的正文挂到 `turns[].thinking.text` 上。这里钉住四件事：

- 写侧不受影响：轨迹外置件与摘要都不带正文（磁盘上不会多出第二份全文）；
- 只挂最近一次有正文的那一轮，前面几轮保持只有读数；
- 正文超过发稿上限时截断并标 `text_truncated`；
- 摘要档与任务快照档都不带正文（那一档是别的读者，体积要守住）。
"""

from __future__ import annotations

from types import SimpleNamespace

from main.models import NodeOutputEntry, NodeRecord
from main.monitoring.execution_trace import (
    THINKING_TEXT_DISPLAY_LIMIT,
    build_execution_trace,
)
from main.monitoring.query_service import TaskQueryService
from main.protocol import now_iso
from main.runtime.execution_trace_compaction import build_execution_trace_summary

TASK_ID = "task:latest-thinking"
NODE_ID = "node:latest-thinking"


def _call(call_id: str) -> dict[str, object]:
    return {"id": call_id, "name": "exec", "arguments": {"command": call_id}}


def _node(reasoning_by_seq: dict[int, str] | None = None) -> NodeRecord:
    reasoning = dict(reasoning_by_seq or {1: "第一轮思考", 2: "第二轮思考\n最后一行：收口"})
    return NodeRecord(
        node_id=NODE_ID,
        task_id=TASK_ID,
        root_node_id=NODE_ID,
        goal="goal",
        prompt="prompt",
        status="success",
        output=[
            NodeOutputEntry(
                seq=1,
                content="first",
                tool_calls=[_call("call-1")],
                reasoning_content=reasoning.get(1, ""),
                thinking={"seconds": 3.0, "chars": 5, "est_tokens": 2} if reasoning.get(1) else {},
                created_at=now_iso(),
            ),
            NodeOutputEntry(
                seq=2,
                content="second",
                tool_calls=[_call("call-2")],
                reasoning_content=reasoning.get(2, ""),
                thinking={"seconds": 4.0, "chars": 15, "est_tokens": 4} if reasoning.get(2) else {},
                created_at=now_iso(),
            ),
            # 最后一轮只有工具、没有思考：底部那一行仍取上一轮。
            NodeOutputEntry(
                seq=3,
                content="",
                tool_calls=[_call("call-3")],
                created_at=now_iso(),
            ),
        ],
        created_at=now_iso(),
        updated_at=now_iso(),
    )


class _DetailRecord:
    node_id = NODE_ID
    task_id = TASK_ID
    execution_trace_ref = ""
    updated_at = "2026-10-29T01:00:00+08:00"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __getattr__(self, name: str) -> str:
        return ""


class _Store:
    def __init__(self, node: NodeRecord) -> None:
        self.node = node
        self.record = _DetailRecord({"node_id": NODE_ID, "task_id": TASK_ID})

    def get_task(self, task_id):
        return SimpleNamespace(task_id=task_id, root_node_id=NODE_ID)

    def get_task_node_detail(self, node_id):
        return self.record

    def get_task_node(self, node_id):
        return None

    def get_node(self, node_id):
        return self.node

    def get_task_node_pause(self, node_id):
        return None

    def list_task_node_tool_results(self, task_id, node_id):
        return []


class _LogService:
    def resolve_node_input_text(self, runtime_node):
        return ""

    def read_runtime_frame(self, task_id, node_id):
        return {}

    def sync_node_read_model(self, task_id, node_id, *, externalize_execution_trace=False):
        return None


def _service(node: NodeRecord, monkeypatch) -> TaskQueryService:
    service = TaskQueryService(store=_Store(node), file_store=None, log_service=_LogService())
    monkeypatch.setattr(service, "_node_message_list", lambda **kwargs: [])
    monkeypatch.setattr(service, "_node_sent_message_list", lambda **kwargs: [])
    return service


def _thinking_by_seq(detail) -> dict[int, dict]:
    return {int(turn.get("seq") or 0): dict(turn.get("thinking") or {}) for turn in (detail.execution_trace or {}).get("turns") or []}


def test_write_side_trace_and_summary_never_carry_the_text() -> None:
    node = _node()
    trace = build_execution_trace(node, tool_results=[], live_tool_calls=[])
    summary = build_execution_trace_summary(trace)

    for payload in (trace, summary):
        for turn in payload["turns"]:
            assert "text" not in (turn.get("thinking") or {}), "写侧多带一份全文，磁盘上就会多一份"


def test_full_detail_attaches_only_the_latest_thinking_text(monkeypatch) -> None:
    service = _service(_node(), monkeypatch)
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level="full")

    thinking = _thinking_by_seq(detail)
    assert thinking[2]["text"] == "第二轮思考\n最后一行：收口"
    assert "text" not in thinking[1], "只挂最近一轮，前面的轮次仍是纯读数"
    assert thinking[3] == {}, "没有思考的那一轮不许凭空长出正文"
    # 读数照旧跟着正文一起走，界面那一行左右两半都靠它。
    assert thinking[2]["seconds"] == 4.0
    assert thinking[2]["est_tokens"] == 4


def test_full_detail_truncates_an_oversized_thinking_text(monkeypatch) -> None:
    oversized = "长" * (THINKING_TEXT_DISPLAY_LIMIT + 10)
    service = _service(_node({1: "第一轮思考", 2: oversized}), monkeypatch)
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level="full")

    thinking = _thinking_by_seq(detail)[2]
    assert len(thinking["text"]) == THINKING_TEXT_DISPLAY_LIMIT
    assert thinking["text_truncated"] is True


def test_summary_and_snapshot_lanes_stay_body_free(monkeypatch) -> None:
    service = _service(_node(), monkeypatch)

    summary_detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level="summary")
    for turn in (summary_detail.execution_trace_summary or {}).get("turns") or []:
        assert "text" not in (turn.get("thinking") or {})

    snapshot_detail = service.get_node_detail(
        TASK_ID,
        NODE_ID,
        detail_level="summary",
        ship_execution_trace_body=False,
    )
    assert not snapshot_detail.execution_trace, "快照档不发轨迹正文"
    for turn in (snapshot_detail.execution_trace_summary or {}).get("turns") or []:
        assert "text" not in (turn.get("thinking") or {}), "快照档也不许带思考正文"


def test_archived_node_without_runtime_record_leaves_the_line_empty(monkeypatch) -> None:
    service = _service(_node(), monkeypatch)
    service._store.node = None
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level="full")
    assert detail.execution_trace.get("turns") in (None, []) or all(
        "text" not in (turn.get("thinking") or {})
        for turn in detail.execution_trace.get("turns") or []
    )
