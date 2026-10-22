from __future__ import annotations

_PLAIN_TEXT_NODE_KINDS = frozenset({'execution', 'acceptance'})


def build_node_plain_text_reply_block_message(*, node_kind: str) -> str:
    """纯文本不是交付：没有工具调用且不是可解析 final 载荷时一律打回。"""
    normalized_kind = str(node_kind or '').strip().lower()
    if normalized_kind not in _PLAIN_TEXT_NODE_KINDS:
        return ''
    work = (
        'call ordinary tools to keep verifying the acceptance criteria'
        if normalized_kind == 'acceptance'
        else 'call ordinary tools (spawn child nodes if needed) to keep advancing'
    )
    return (
        'This round output plain text with no tool call. The runtime does not treat it as a delivery and will not '
        'submit anything on your behalf. '
        f'The next step is exactly one of two: {work}; '
        'if the goal is done, call `submit_final_result` to submit properly (put `status`, `answer` and '
        '`evidence` all in the tool arguments). '
        'Writing plans, progress notes or "what I will do next" into the reply body counts as no submission at all.'
    )
