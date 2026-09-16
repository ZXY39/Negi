"""g3ku.audit_events 事件池单元测试。

覆盖：配置与落盘、坏输入规范化、超大 detail 截断、未配置语义、
读取过滤与分页（since 严格大于）、24h 概览窗口、保留期修剪。
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from g3ku import audit_events


@pytest.fixture()
def sink(tmp_path: Path):
    """配置 tmp 工作区为审计池，测试后复位为未配置。"""
    workspace = tmp_path / "workspace"
    audit_events.configure_audit_sink(workspace)
    yield workspace
    audit_events.configure_audit_sink(None)


def _audit_lines(workspace: Path) -> list[dict]:
    audit_file = workspace / ".g3ku" / "audit.jsonl"
    if not audit_file.exists():
        return []
    raw = audit_file.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _ago(seconds: float) -> str:
    """相对真实时钟的 ISO 时间戳，保证落在 24h 概览窗口内。"""
    return (datetime.now().astimezone() - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _make_stamp_sequence(stamps: list[str]):
    """把给定时间戳依次喂给 _now_iso；序列耗尽后回落到真实 _now_iso。"""
    remaining = list(stamps)
    real_now = audit_events._now_iso

    def stamp():
        if remaining:
            return remaining.pop(0)
        return real_now()

    return stamp


def test_configure_and_emit_writes_single_line_record(sink: Path) -> None:
    configured = audit_events.audit_file_path()
    assert configured is not None
    assert configured == sink / ".g3ku" / "audit.jsonl"

    assert (
        audit_events.emit_audit_event(
            "provider",
            "error",
            "provider_chain_exhausted",
            "模型链耗尽：上游 429",
            detail={"retryable": True},
        )
        is True
    )

    lines = _audit_lines(sink)
    assert len(lines) == 1
    record = lines[0]
    assert set(record.keys()) == {
        "event_id",
        "timestamp",
        "subsystem",
        "level",
        "event_type",
        "summary",
        "detail",
    }
    assert record["event_id"].startswith("evt_")
    assert record["subsystem"] == "provider"
    assert record["level"] == "error"
    assert record["event_type"] == "provider_chain_exhausted"
    assert record["summary"] == "模型链耗尽：上游 429"
    assert record["detail"] == {"retryable": True}
    # ensure_ascii=False：中文摘要原文可读落盘
    raw_text = (sink / ".g3ku" / "audit.jsonl").read_text(encoding="utf-8")
    assert "模型链耗尽" in raw_text


def test_emit_normalizes_bad_inputs(sink: Path) -> None:
    assert audit_events.emit_audit_event(" Provider ", "BOGUS", "", "  " * 5) is True
    record = _audit_lines(sink)[0]
    assert record["subsystem"] == "provider"
    assert record["level"] == "info"
    assert record["event_type"] == "unspecified"

    # 摘要超长截断到 240 字符
    assert audit_events.emit_audit_event("task", "error", "x", "字" * 300) is True
    assert len(_audit_lines(sink)[-1]["summary"]) == 240


def test_emit_truncates_oversized_detail(sink: Path) -> None:
    assert (
        audit_events.emit_audit_event(
            "task", "error", "task_node_error", "节点错误", detail={"blob": "x" * 5000}
        )
        is True
    )
    detail = _audit_lines(sink)[0]["detail"]
    assert detail["detail_truncated"] is True
    assert len(detail["detail_text"]) == 1000


def test_emit_unconfigured_is_noop(tmp_path: Path, monkeypatch) -> None:
    # 未配置时绝不写任何文件（无 cwd 兜底，防止污染仓库根）
    monkeypatch.chdir(tmp_path)
    audit_events.configure_audit_sink(None)
    assert audit_events.audit_file_path() is None
    assert audit_events.emit_audit_event("provider", "error", "x", "y") is False
    assert not (tmp_path / ".g3ku").exists()


def test_list_orders_newest_first_with_pagination(sink: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        audit_events, "_now_iso", _make_stamp_sequence([_ago(60 + i) for i in range(5)])
    )
    for index in range(5):
        assert audit_events.emit_audit_event("provider", "info", "e", f"事件{index}") is True

    page = audit_events.list_audit_events(limit=2, offset=0)
    assert page["total"] == 5
    assert page["has_more"] is True
    assert [item["summary"] for item in page["items"]] == ["事件4", "事件3"]

    tail = audit_events.list_audit_events(limit=2, offset=4)
    assert tail["has_more"] is False
    assert [item["summary"] for item in tail["items"]] == ["事件0"]


def test_list_filters_by_level_and_subsystem(sink: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        audit_events, "_now_iso", _make_stamp_sequence([_ago(3 + i) for i in range(3)])
    )
    audit_events.emit_audit_event("provider", "error", "a", "p-err")
    audit_events.emit_audit_event("task", "warning", "b", "t-warn")
    audit_events.emit_audit_event("provider", "info", "c", "p-info")

    assert audit_events.list_audit_events(level="error")["total"] == 1
    assert audit_events.list_audit_events(subsystem="provider")["total"] == 2
    assert audit_events.list_audit_events(level="error", subsystem="task")["total"] == 0


def test_list_since_is_strictly_greater(sink: Path, monkeypatch) -> None:
    stamps = [_ago(30), _ago(20), _ago(10)]
    monkeypatch.setattr(audit_events, "_now_iso", _make_stamp_sequence(stamps))
    for summary in ("老事件", "中事件", "新事件"):
        audit_events.emit_audit_event("provider", "info", "x", summary)

    # since=中间事件时间戳：严格大于 → 只有最新一条
    page = audit_events.list_audit_events(since=stamps[1])
    assert page["total"] == 1
    assert page["items"][0]["summary"] == "新事件"

    # since=最新时间戳（相等被排除）：0 条 —— 角标不重复计数
    assert audit_events.list_audit_events(since=stamps[2])["total"] == 0


def test_list_skips_malformed_lines(sink: Path) -> None:
    audit_file = sink / ".g3ku" / "audit.jsonl"
    with audit_file.open("a", encoding="utf-8") as fh:
        fh.write("完全不是 json\n")
        fh.write(json.dumps({"没有": "timestamp"}) + "\n")
    audit_events.emit_audit_event("provider", "info", "x", "唯一有效事件")

    page = audit_events.list_audit_events()
    assert page["total"] == 1
    assert page["items"][0]["summary"] == "唯一有效事件"


def test_list_raises_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(audit_events, "_lazy_workspace_root", lambda: None)
    audit_events.configure_audit_sink(None)
    with pytest.raises(RuntimeError, match="audit_sink_unconfigured"):
        audit_events.list_audit_events()
    with pytest.raises(RuntimeError, match="audit_sink_unconfigured"):
        audit_events.audit_summary()


def test_summary_zero_fills_fixed_subsystems(sink: Path) -> None:
    summary = audit_events.audit_summary()
    subsystems = summary["subsystems"]
    assert [entry["subsystem"] for entry in subsystems] == ["provider", "task", "web_api"]
    for entry in subsystems:
        assert entry["status"] == "ok"
        assert entry["event_count"] == 0
        assert entry["error_count"] == 0
        assert entry["warning_count"] == 0
        assert entry["latest_event_at"] == ""
        assert entry["latest_event_summary"] == ""
    assert summary["generated_at"]


def test_summary_counts_and_flips_status(sink: Path, monkeypatch) -> None:
    stamps = [_ago(9), _ago(6), _ago(3)]
    monkeypatch.setattr(audit_events, "_now_iso", _make_stamp_sequence(stamps))
    audit_events.emit_audit_event("provider", "error", "a", "provider 出错", detail={"n": 1})
    audit_events.emit_audit_event("task", "warning", "b", "任务告警")
    audit_events.emit_audit_event("provider", "warning", "c", "调用警告")

    summary = audit_events.audit_summary()
    by_key = {entry["subsystem"]: entry for entry in summary["subsystems"]}
    provider = by_key["provider"]
    assert provider["status"] == "error"
    assert provider["event_count"] == 2
    assert provider["error_count"] == 1
    assert provider["warning_count"] == 1
    assert provider["latest_event_at"] == stamps[2]
    assert provider["latest_event_level"] == "warning"
    assert provider["latest_event_summary"] == "调用警告"
    task = by_key["task"]
    assert task["status"] == "ok"
    assert task["warning_count"] == 1


def test_summary_window_excludes_old_events(sink: Path, monkeypatch) -> None:
    old_stamp = (datetime.now().astimezone() - timedelta(hours=25)).isoformat(timespec="seconds")
    monkeypatch.setattr(audit_events, "_now_iso", _make_stamp_sequence([old_stamp]))
    audit_events.emit_audit_event("provider", "error", "x", "25 小时前的错误")

    summary = audit_events.audit_summary()
    provider = next(entry for entry in summary["subsystems"] if entry["subsystem"] == "provider")
    assert provider["status"] == "ok"
    assert provider["error_count"] == 0
    assert provider["event_count"] == 0


def test_trim_keeps_newest_events(sink: Path, monkeypatch) -> None:
    # 压缩上限让每约 3 条就触发一次修剪：保留最新 5 条
    monkeypatch.setattr(audit_events, "AUDIT_MAX_FILE_BYTES", 600)
    monkeypatch.setattr(audit_events, "AUDIT_TRIM_KEEP_EVENTS", 5)
    monkeypatch.setattr(
        audit_events, "_now_iso", _make_stamp_sequence([_ago(300 - i) for i in range(30)])
    )
    for index in range(30):
        assert audit_events.emit_audit_event("provider", "info", "x", f"事件{index:02d}") is True

    lines = _audit_lines(sink)
    assert [record["summary"] for record in lines] == [
        f"事件{index:02d}" for index in range(25, 30)
    ]

    # 修剪用 pid 后缀临时文件 + os.replace，成功路径不得残留 tmp
    leftovers = [p.name for p in (sink / ".g3ku").iterdir() if "audit.jsonl.tmp" in p.name]
    assert leftovers == []
