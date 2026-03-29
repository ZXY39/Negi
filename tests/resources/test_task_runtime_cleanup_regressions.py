from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = [REPO_ROOT / "main", REPO_ROOT / "g3ku"]
BANNED_PATTERNS = [
    "task.snapshot",
    "task.list.snapshot",
    "task.list.patch",
    "TaskProjectionService",
    "TaskTreeBuilder",
    "TaskRunner(",
    "_snapshot_payload_builder",
    "runtime_state_path",
    "tree_snapshot_path",
    "tree_text_path",
    "runtime_states",
]


def _iter_source_files():
    for base in SOURCE_DIRS:
        for path in base.rglob("*"):
            if path.suffix.lower() not in {".py", ".js", ".ts", ".tsx", ".md"}:
                continue
            yield path


def test_task_runtime_v2_cleanup_patterns_are_absent_from_source_tree() -> None:
    matches: list[str] = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in BANNED_PATTERNS:
            if pattern in text:
                matches.append(f"{path.relative_to(REPO_ROOT)} -> {pattern}")
    assert matches == []
