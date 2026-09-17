"""验收裁定的文件证据审计契约。

背景（2026-09-17 task:77d0ae460cf0）：验收裁定把不存在的路径（data-005.pdf / hardware-001.pdf，
是按"类别+序号"编出来的名字）当成"对方少交付/造假"的证据，并把内容工具对二进制文件返回的
占位串长度当成"实测文件大小"。此审计在裁定时刻机械记录每条 file 证据的磁盘真值，
让这类争议事后有据可查；判定门槛本身由验收提示词的证据纪律约束。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from main.models import NodeFinalResult, SpawnChildSpec
from main.runtime.node_runner import _ACCEPTANCE_EVIDENCE_AUDIT_KEY
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called: {kwargs!r}")


def _make_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service._assert_worker_available = lambda: None
    return service


def _create_execution_child(service: MainRuntimeService, *, task, parent, name: str = "child"):
    return service.node_runner._create_execution_child(
        task=task,
        parent=parent,
        spec=SpawnChildSpec(
            goal=f"{name} goal",
            prompt=f"{name} prompt",
            execution_policy={"mode": "focus"},
        ),
    )


def _verdict(*, status: str = "failed", evidence: list | None = None) -> NodeFinalResult:
    return NodeFinalResult(
        status=status,
        delivery_status="final",
        summary="验收结论",
        answer="验收结论",
        evidence=list(evidence or []),
        remaining_work=[],
        blocking_reason="" if status == "success" else "交付物不合格",
    )


async def _node_for_audit(service: MainRuntimeService):
    record = await service.create_task("acceptance audit task", session_id="web:shared")
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    child = _create_execution_child(service, task=task, parent=root)
    return task, service.get_node(child.node_id)


def _audit_records(service: MainRuntimeService, node_id: str) -> list[dict]:
    node = service.get_node(node_id)
    return list((node.metadata or {}).get(_ACCEPTANCE_EVIDENCE_AUDIT_KEY) or [])


@pytest.mark.asyncio
async def test_audit_records_disk_truth_for_cited_file_evidence(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _task, node = await _node_for_audit(service)
    real = tmp_path / "delivered.pdf"
    real.write_bytes(b"%PDF-1.4\n" + b"x" * 500)
    missing = tmp_path / "invented-005.pdf"

    service.node_runner._record_acceptance_evidence_audit(
        acceptance=node,
        result=_verdict(
            evidence=[
                {"kind": "file", "path": str(real), "note": "PDF 仅 34 字节，空壳"},
                {"kind": "file", "path": str(missing), "note": "路径不存在"},
            ]
        ),
    )

    records = _audit_records(service, node.node_id)
    assert len(records) == 1
    cited = {item["path"]: item for item in records[0]["cited_files"]}
    assert cited[str(real)]["exists"] is True
    assert cited[str(real)]["size_bytes"] == real.stat().st_size
    assert "mtime" in cited[str(real)]
    assert cited[str(missing)]["exists"] is False
    assert records[0]["missing_count"] == 1
    assert records[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_audit_ignores_non_file_evidence_and_blank_paths(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _task, node = await _node_for_audit(service)

    service.node_runner._record_acceptance_evidence_audit(
        acceptance=node,
        result=_verdict(
            evidence=[
                {"kind": "artifact", "ref": "artifact:abc", "note": "结果载荷"},
                {"kind": "file", "path": "   ", "note": "空路径"},
                {"kind": "url", "path": "https://example.com", "note": "外链"},
            ]
        ),
    )

    assert _audit_records(service, node.node_id) == []


@pytest.mark.asyncio
async def test_audit_history_is_bounded(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _task, node = await _node_for_audit(service)
    evidence = [{"kind": "file", "path": str(tmp_path / "missing.bin"), "note": "x"}]

    for _ in range(7):
        service.node_runner._record_acceptance_evidence_audit(
            acceptance=node,
            result=_verdict(evidence=evidence),
        )

    assert len(_audit_records(service, node.node_id)) == 5


@pytest.mark.asyncio
async def test_acceptance_verdict_path_records_audit(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task, node = await _node_for_audit(service)
    real = tmp_path / "delivered.pdf"
    real.write_bytes(b"%PDF-1.4\n")
    finalized: list[str] = []
    service.node_runner._finalize_acceptance_pass = lambda **kwargs: finalized.append(
        str(kwargs.get("acceptance").node_id)
    )

    service.node_runner._handle_acceptance_node_result(
        task=task,
        acceptance=node,
        result=_verdict(status="success", evidence=[{"kind": "file", "path": str(real), "note": "核验"}]),
    )

    assert finalized == [node.node_id]
    records = _audit_records(service, node.node_id)
    assert records and records[0]["cited_files"][0]["exists"] is True
