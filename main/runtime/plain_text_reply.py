from __future__ import annotations

_PLAIN_TEXT_NODE_KINDS = frozenset({'execution', 'acceptance'})


def build_node_plain_text_reply_block_message(*, node_kind: str, continuous: bool = False) -> str:
    """纯文本不是交付：没有工具调用且不是可解析 final 载荷时一律打回。

    连续节点的合法出口是「继续调工具 / 收到结束指令才提交 / 否则停靠待命」；
    其它节点保持原出口清单，避免同一句提醒在两条车道上漂移。
    """
    normalized_kind = str(node_kind or '').strip().lower()
    if normalized_kind not in _PLAIN_TEXT_NODE_KINDS:
        return ''
    if normalized_kind == 'acceptance':
        work = 'call ordinary tools to keep verifying the acceptance criteria'
        finish = (
            'if the goal is done, call `submit_final_result` to submit properly (put `status`, `answer` and '
            '`evidence` all in the tool arguments)'
        )
    elif bool(continuous):
        work = 'call ordinary tools to keep advancing'
        finish = (
            'when the assigned work is done, call `enter_wait(waiting_for=["frontdoor"])` to stand by '
            'for the next message or event; you have no submit or acceptance channel — to end your duty, '
            'use `send_message` to your upstream instead of replying plain text'
        )
    else:
        work = 'call ordinary tools (spawn child nodes if needed) to keep advancing'
        finish = (
            'if the goal is done, call `submit_final_result` to submit properly (put `status`, `answer` and '
            '`evidence` all in the tool arguments)'
        )
    return (
        'This round output plain text with no tool call. The runtime does not treat it as a delivery and will not '
        'submit anything on your behalf. '
        f'The next step is exactly one of two: {work}; '
        f'{finish}. '
        'Writing plans, progress notes or "what I will do next" into the reply body counts as no submission at all.'
    )
