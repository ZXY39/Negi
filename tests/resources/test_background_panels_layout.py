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


def test_session_bubble_centers_elapsed_and_stop_against_wrapped_source() -> None:
    row = _rule(_css(), ".ceo-bg-jobs-row")
    assert "grid-template-columns: minmax(0, 1fr) auto auto;" in row
    assert "align-items: center;" in row


def test_session_process_command_fallback_clamps_to_two_lines() -> None:
    command = _rule(_css(), ".ceo-bg-jobs-command")
    assert "-webkit-line-clamp: 2;" in command
    assert "line-clamp: 2;" in command


def test_task_process_rows_put_stop_right_of_node_button_at_the_same_height() -> None:
    row = _rule(_css(), ".task-processes-row")
    # 四列：主列 / 时长 / 定位节点 / 停止。
    assert "grid-template-columns: minmax(0, 1fr) auto auto auto;" in row
    assert "align-items: center;" in row
    # 停止按钮与节点胶囊同高：同字号、同纵向内边距，只保留更宽的点击区。
    stop = _rule(_css(), ".task-processes-stop")
    node = _rule(_css(), ".task-processes-node")
    assert "font-size: 0.68rem;" in stop
    assert "padding: 1px 9px;" in stop
    assert "font-size: 0.68rem;" in node
    assert "padding: 1px 7px;" in node


def test_task_process_command_fallback_clamps_to_two_lines() -> None:
    command = _rule(_css(), ".task-processes-main--command")
    assert "-webkit-line-clamp: 2;" in command
    assert "line-clamp: 2;" in command
