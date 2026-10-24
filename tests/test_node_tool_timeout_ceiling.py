"""H3: node-lane tool calls are clamped to the machine ceiling and told so."""

from __future__ import annotations

from main.runtime.react_loop import resolve_node_tool_timeout

NODE_CONTEXT = {'node_tool_timeout_ceiling_seconds': 900.0}


class TestClamp:
    def test_long_request_is_clamped_and_explained(self):
        seconds, note = resolve_node_tool_timeout({'timeout_seconds': 99999}, NODE_CONTEXT)
        assert seconds == 900.0
        assert '900' in note
        assert '拆成子节点' in note

    def test_request_under_the_ceiling_is_untouched(self):
        seconds, note = resolve_node_tool_timeout({'timeout_seconds': 120}, NODE_CONTEXT)
        assert seconds == 120.0
        assert note == ''

    def test_ceo_context_has_no_ceiling(self):
        # CEO 车道的 runtime_context 没有这个键：H3 只约束节点道。
        seconds, note = resolve_node_tool_timeout({'timeout_seconds': 99999}, {})
        assert seconds == 99999.0
        assert note == ''

    def test_invalid_ceiling_is_ignored(self):
        seconds, note = resolve_node_tool_timeout(
            {'timeout_seconds': 5000}, {'node_tool_timeout_ceiling_seconds': 'oops'}
        )
        assert seconds == 5000.0
        assert note == ''

    def test_default_timeout_is_clamped_too(self):
        # 不传 timeout_seconds 时走全局默认；默认值同样不得穿透 H3 上限。
        seconds, _note = resolve_node_tool_timeout(
            {}, {'node_tool_timeout_ceiling_seconds': 1.0}
        )
        assert seconds == 1.0
