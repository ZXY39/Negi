from __future__ import annotations

import pytest

from g3ku.runtime.cancellation import ToolCancellationRequested, ToolCancellationToken


def test_child_token_cancel_does_not_cancel_parent() -> None:
    parent = ToolCancellationToken(session_key="web:shared")
    child = parent.derive_child(reason="inline_tool:exec")

    child.cancel(reason="sidecar_timeout_stop")

    assert child.is_cancelled() is True
    assert parent.is_cancelled() is False
    with pytest.raises(ToolCancellationRequested, match="sidecar_timeout_stop"):
        child.raise_if_cancelled()


def test_parent_cancellation_propagates_to_existing_child() -> None:
    parent = ToolCancellationToken(session_key="web:shared")
    child = parent.derive_child(reason="inline_tool:exec")

    parent.cancel(reason="user_pause")

    assert parent.is_cancelled() is True
    assert child.is_cancelled() is True
    with pytest.raises(ToolCancellationRequested, match="user_pause"):
        child.raise_if_cancelled()
