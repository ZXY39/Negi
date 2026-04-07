from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class RecoveryCheckDecision(StrEnum):
    VERIFIED_DONE = "verified_done"
    RERUN_SAFE = "rerun_safe"
    MODEL_DECIDE = "model_decide"


@dataclass(slots=True)
class RecoveryCheckResult:
    decision: RecoveryCheckDecision
    expected_tool_status: str = ""
    lost_result_summary: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)


class RecoveryCheckEngine:
    def __init__(self, *, workspace_root: Path | str) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve(strict=False)

    def inspect_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None,
        runtime_context: dict[str, Any] | None,
    ) -> RecoveryCheckResult:
        normalized_tool_name = str(tool_name or "").strip().lower()
        payload = dict(arguments or {})
        _runtime = dict(runtime_context or {})
        if normalized_tool_name == "filesystem":
            return self._inspect_filesystem_call(payload)
        if normalized_tool_name in {"exec", "shell"}:
            return RecoveryCheckResult(
                decision=RecoveryCheckDecision.MODEL_DECIDE,
                expected_tool_status="interrupted",
                lost_result_summary=(
                    "The previous exec/shell attempt may have produced external side effects. "
                    "The model must verify whether the previous side effect already completed before retrying."
                ),
            )
        return RecoveryCheckResult(
            decision=RecoveryCheckDecision.RERUN_SAFE,
            lost_result_summary="The interrupted tool call has no known durable side effect and is safe to rerun.",
        )

    def _inspect_filesystem_call(self, arguments: dict[str, Any]) -> RecoveryCheckResult:
        action = str(arguments.get("action") or "").strip().lower()
        if action == "write":
            return self._inspect_filesystem_write(arguments)
        if action == "edit":
            return self._inspect_filesystem_edit(arguments)
        if action in {"describe", "search", "open", "head", "tail", "list"}:
            return RecoveryCheckResult(
                decision=RecoveryCheckDecision.RERUN_SAFE,
                lost_result_summary="The interrupted filesystem read action is safe to rerun.",
            )
        if action == "propose_patch":
            return RecoveryCheckResult(
                decision=RecoveryCheckDecision.MODEL_DECIDE,
                expected_tool_status="interrupted",
                lost_result_summary=(
                    "The previous patch proposal may already have been applied or partially applied. "
                    "The model must verify the target state before retrying."
                ),
            )
        return RecoveryCheckResult(
            decision=RecoveryCheckDecision.MODEL_DECIDE,
            expected_tool_status="interrupted",
            lost_result_summary=(
                "The interrupted filesystem mutation could not be classified safely. "
                "The model must verify the current target state before retrying."
            ),
        )

    def _inspect_filesystem_write(self, arguments: dict[str, Any]) -> RecoveryCheckResult:
        target = self._resolve_path(arguments.get("path"))
        expected = str(arguments.get("content") or "")
        if target is not None and target.is_file():
            try:
                current = target.read_text(encoding="utf-8")
            except Exception:
                current = None
            if current == expected:
                return RecoveryCheckResult(
                    decision=RecoveryCheckDecision.VERIFIED_DONE,
                    expected_tool_status="success",
                    lost_result_summary=(
                        "Recovery check confirmed that the target file already matches requested content."
                    ),
                    evidence=[self._file_evidence(path=target, note="File content matches the interrupted write request.")],
                )
        return RecoveryCheckResult(
            decision=RecoveryCheckDecision.MODEL_DECIDE,
            expected_tool_status="interrupted",
            lost_result_summary=(
                "The interrupted filesystem.write request could not be proven complete. "
                "Verify the file state before retrying."
            ),
        )

    def _inspect_filesystem_edit(self, arguments: dict[str, Any]) -> RecoveryCheckResult:
        target = self._resolve_path(arguments.get("path"))
        old_text = str(arguments.get("old_text") or "")
        new_text = str(arguments.get("new_text") or "")
        if target is not None and target.is_file():
            try:
                current = target.read_text(encoding="utf-8")
            except Exception:
                current = None
            if current is not None and old_text not in current and new_text and new_text in current:
                return RecoveryCheckResult(
                    decision=RecoveryCheckDecision.VERIFIED_DONE,
                    expected_tool_status="success",
                    lost_result_summary=(
                        "Recovery check confirmed that the requested edit is already reflected on disk."
                    ),
                    evidence=[self._file_evidence(path=target, note="File content already reflects the requested edit.")],
                )
        return RecoveryCheckResult(
            decision=RecoveryCheckDecision.MODEL_DECIDE,
            expected_tool_status="interrupted",
            lost_result_summary=(
                "The interrupted filesystem.edit request could not be proven complete. "
                "Verify the file state before retrying."
            ),
        )

    def _resolve_path(self, raw: Any) -> Path | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            return Path(text).expanduser().resolve(strict=False)
        except Exception:
            return None

    @staticmethod
    def _file_evidence(*, path: Path, note: str) -> dict[str, Any]:
        return {
            "kind": "file",
            "path": str(path),
            "ref": "",
            "start_line": None,
            "end_line": None,
            "note": str(note or "").strip(),
        }
