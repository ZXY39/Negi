from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CSS_PATH = REPO_ROOT / "g3ku/web/frontend/org_graph.css"


def _rule(css: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert match, f"缺少规则块：{selector}"
    return match.group(1)


def _css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def test_session_bubble_keeps_elapsed_and_stop_on_the_first_line() -> None:
    row = _rule(_css(), ".ceo-bg-jobs-row")
    assert "grid-template-columns: minmax(0, 1fr) auto auto;" in row
    assert "align-items: start;" in row


def test_session_process_command_fallback_clamps_to_two_lines() -> None:
    command = _rule(_css(), ".ceo-bg-jobs-command")
    assert "-webkit-line-clamp: 2;" in command
    assert "line-clamp: 2;" in command


def test_task_process_rows_put_node_button_right_of_stop_on_the_first_line() -> None:
    row = _rule(_css(), ".task-processes-row")
    # 五列：来源标签 / 主列 / 时长 / 停止 / 定位节点。
    assert "grid-template-columns: auto minmax(0, 1fr) auto auto auto;" in row
    assert "align-items: start;" in row


def test_task_process_command_fallback_clamps_to_two_lines() -> None:
    command = _rule(_css(), ".task-processes-main--command")
    assert "-webkit-line-clamp: 2;" in command
    assert "line-clamp: 2;" in command
