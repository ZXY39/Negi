from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "g3ku" / "web" / "frontend"


def _read(*parts: str) -> str:
    return (FRONTEND.joinpath(*parts)).read_text(encoding="utf-8")


def _slice(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    tail = text[start:]
    end = tail.index(end_marker)
    return tail[:end]


def test_accept_confirm_only_closes_the_dialog_it_owns() -> None:
    # `onConfirm` 里 resolve 时，等待这张弹窗的调用方拿到的是第一个微任务：串联下一张
    # 弹窗在这句收尾之前就建好并换了 S.confirmState。收尾若照旧无条件执行，拆掉的是那张
    # 新弹窗，还会触发它的 onClose 把用户判成"取消"，操作于是无声失败（请求不发、toast 不弹）。
    confirm_js = _read("ui", "confirm.js")
    accept_body = _slice(confirm_js, "async function acceptConfirm()", "\n}\n")

    assert "const state = S.confirmState;" in accept_body
    assert re.search(r"await state\.onConfirm\(", accept_body)
    assert "if (S.confirmState === state) closeConfirm();" in accept_body
    assert re.search(r"await state\.onConfirm\([^;]*\);\s*closeConfirm\(\);", accept_body) is None


def test_clear_flow_opens_both_confirms_in_order_and_reports_progress() -> None:
    js = _read("org_graph_external.js")
    flow = _slice(js, "async function _clearQqGroupHistory()", "\n// ---- 群聊设置弹窗")

    # 过期占用条不能静默返回：这是"按钮坏了"最像的一种表现。
    guard = _slice(flow, "if (!sessionId)", "return;\n    }")
    assert "showToast(" in guard

    # 进度 toast 必须包在请求外面，并在 finally 撤掉计时器。
    assert flow.index("_startQqHistoryClearProgress()") < flow.index("ApiClient.clearQqGroupHistory")
    assert "stopProgress();" in _slice(flow, "} finally {", "\n}")
    progress = _slice(js, "function _startQqHistoryClearProgress()", "\n}")
    assert "persistent: true" in progress
    assert "spin: true" in progress
    # 每秒改文本节点，不重建整条 toast（图标会被反复重建）。
    assert "app-toast-text" in progress
    assert "showToast" not in progress[progress.index("setInterval"):]


def test_clear_request_has_no_client_timeout() -> None:
    js = _read("client_api", "external.js")
    body = _slice(js, "async clearQqGroupHistory(sessionId)", "\n    }")

    # 群文件上限按 2GB 配，删除时长由磁盘决定：客户端超时只会把删成功报成失败。
    assert '_request("POST", "/api/qq-bot/history/clear"' in body
    assert "timeoutMs: 0" in body


def test_busy_and_wrong_shape_codes_read_as_operator_sentences() -> None:
    # 后端这两个 detail 码是裸标识符，直接进 toast 就是工程黑话。
    core_js = _read("client_api", "core.js")
    friendly = _slice(core_js, "static _friendlyErrorMessageForCode(code)", "default:")

    assert 'case "session_busy"' in friendly
    assert 'case "qq_group_session_required"' in friendly
