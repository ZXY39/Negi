"""Read-only child inspection tools for execution nodes.

Three tools over one snapshot shape so the model sees the same facts whichever
door it comes through: a child's state is always the node row plus its runtime
frame, never a projection that could lag. Progress is derived from what the
child actually produced (its last assistant text, its tool calls, its acceptance
rounds) — there is deliberately no field the child has to write for a parent to
understand it.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from g3ku.agent.tools.base import Tool
from main.runtime.tool_names import (
    LIST_CHILD_NODES_TOOL_NAME,
    MANAGE_CHILD_NODES_TOOL_NAME,
    READ_CHILD_PROGRESS_TOOL_NAME,
    VIEW_NODE_TREE_TOOL_NAME,
)


class ListChildNodesTool(Tool):
    """直接子节点清单：一行一个，带状态与未读数。"""

    hide_universal_timeout_parameter = True

    def __init__(self, list_children: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self._list_children = list_children

    @property
    def name(self) -> str:
        return LIST_CHILD_NODES_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'List your direct child nodes with their state, how many unread messages each has, '
            'and whether it has settled. Use it to see who is still outstanding; you do not need '
            'it to know when they finish — an unsettled child reports to you by message.'
        )

    @property
    def model_description(self) -> str:
        return 'List direct children with state and unread counts.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'state': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Optional filter, e.g. ["running"] or ["paused"].',
                },
                'settled': {
                    'type': 'boolean',
                    'description': 'Optional: true for finished children only, false for live ones.',
                },
            },
            'required': [],
        }

    async def execute(self, state: Any = None, settled: Any = None, **kwargs: Any) -> str:
        _ = kwargs
        payload = await self._list_children(state=state, settled=settled)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ReadChildProgressTool(Tool):
    """一个子节点的进度：最近文本、近期工具流水、验收轮次、看门狗判停史。"""

    hide_universal_timeout_parameter = True

    def __init__(self, read_progress: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self._read_progress = read_progress

    @property
    def name(self) -> str:
        return READ_CHILD_PROGRESS_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'Read what one direct child is actually doing: its latest assistant text, its recent '
            'tool calls, how many acceptance rounds it has been through and the last verdict, and '
            'any watchdog pauses it hit. Pointers to large bodies are returned instead of the '
            'bodies themselves.'
        )

    @property
    def model_description(self) -> str:
        return 'Inspect one child: latest text, recent tools, acceptance rounds, watchdog history.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'node_id': {'type': 'string', 'description': 'Direct child node id.', 'minLength': 1},
                'focus': {'type': 'string', 'description': 'Optional: what you care about.'},
            },
            'required': ['node_id'],
        }

    async def execute(self, node_id: str = '', focus: str = '', **kwargs: Any) -> str:
        _ = kwargs
        payload = await self._read_progress(node_id=str(node_id or '').strip(), focus=focus)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ViewNodeTreeTool(Tool):
    """以本节点为根的整棵子树，一行一个节点。"""

    hide_universal_timeout_parameter = True

    def __init__(self, view_tree: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self._view_tree = view_tree

    @property
    def name(self) -> str:
        return VIEW_NODE_TREE_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'One line per node, with goal, id, state, kind, acceptance status and whether you may '
            'message or control it. `scope=subtree` walks your descendants; `scope=family` also '
            'shows your parent and your direct siblings. Acceptance nodes stay visible but are '
            'marked non-messageable/non-controllable. Depth and total node count are bounded; when '
            'the result is too large it says so and gives a pointer instead of half a tree.'
        )

    @property
    def model_description(self) -> str:
        return 'Bounded tree/contacts view: parent, self, siblings and descendants, with messageable/controllable flags.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'scope': {
                    'type': 'string',
                    'enum': ['subtree', 'family'],
                    'description': (
                        'subtree = your descendants only; family = parent + self + direct siblings + '
                        'your descendants.'
                    ),
                },
                'depth': {'type': 'integer', 'minimum': 1, 'description': 'How deep to walk.'},
            },
            'required': [],
        }

    async def execute(self, depth: Any = None, scope: str = 'subtree', **kwargs: Any) -> str:
        _ = kwargs
        payload = await self._view_tree(depth=depth, scope=scope)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ManageChildNodesTool(Tool):
    """批量处理直接子节点：暂停、恢复、终止。

    `resume` 走到父→子消息通道：系统代父节点写一条 `parent_notice`（正文 =
    `note`）并唤醒子节点，恢复与修复信息一次完成。对正在等待的子节点 `resume`
    会被打回——等待由唤醒源驱动，不需要也不应该用 resume 干预。

    整批校验是同步的：任一条 `terminate` 缺 `reason` 打回整次调用，避免
    "前几条已生效、后面才发现参数不全"的半批副作用。
    """

    hide_universal_timeout_parameter = True

    def __init__(self, manage_children: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self._manage_children = manage_children

    @property
    def name(self) -> str:
        return MANAGE_CHILD_NODES_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            'Pause, resume, or terminate your direct execution child nodes in one batch. `pause` is '
            'cooperative: the child finishes the tool call it is in and starts no new turn. '
            '`resume` writes a message to the child and wakes it — put the fix or instruction in '
            '`note`; it is rejected for a child that is merely waiting (a waiting node continues '
            'when its own wake source delivers). `terminate` ends the child and its whole subtree, '
            'including descendant acceptance nodes, and requires `reason`. Acceptance children are '
            'not controllable here. Results are per item and independent.'
        )

    @property
    def model_description(self) -> str:
        return 'Pause, resume, or terminate direct children; terminate needs a reason.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'ops': {
                    'type': 'array',
                    'minItems': 1,
                    'items': {
                        'type': 'object',
                        'properties': {
                            'node_id': {
                                'type': 'string',
                                'minLength': 1,
                                'description': 'Direct child node id.',
                            },
                            'action': {
                                'type': 'string',
                                'enum': ['pause', 'resume', 'terminate'],
                            },
                            'note': {
                                'type': 'string',
                                'description': 'For resume: the message that wakes the child.',
                            },
                            'reason': {
                                'type': 'string',
                                'description': 'Required for terminate: why it is given up.',
                            },
                        },
                        'required': ['node_id', 'action'],
                    },
                },
            },
            'required': ['ops'],
        }

    async def execute(self, ops: Any = None, **kwargs: Any) -> str:
        _ = kwargs
        items = list(ops or []) if isinstance(ops, (list, tuple)) else []
        if not items:
            return json.dumps(
                {'ok': False, 'error': 'invalid_param', 'details': 'ops must contain at least one entry'},
                ensure_ascii=False,
                sort_keys=True,
            )
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                return json.dumps(
                    {'ok': False, 'error': 'invalid_param', 'details': f'ops[{position}] must be an object'},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            action = str(item.get('action') or '').strip().lower()
            if action == 'terminate' and not str(item.get('reason') or '').strip():
                return json.dumps(
                    {
                        'ok': False,
                        'error': 'invalid_param',
                        'details': f'ops[{position}].reason is required for terminate',
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
        payload = await self._manage_children(ops=items)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
