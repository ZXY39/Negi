"""节点侧 `tools[]` 重印边界只认压缩，不认阶段收口形状。

时间量不参与判定：这条车道没有任何"多久没发过"的读数点。
"""

from __future__ import annotations

from main.runtime.react_loop import ReActToolLoop


def test_node_recommit_reason_only_accepts_compression() -> None:
    decide = ReActToolLoop._node_bundle_recommit_reason

    assert decide(history_shrink_reason="token_compression") == "token_compression"
    assert decide(history_shrink_reason="stage_compaction") == ""
    assert decide(history_shrink_reason="") == ""
