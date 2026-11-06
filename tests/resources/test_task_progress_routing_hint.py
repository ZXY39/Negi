"""`task_progress` 的返回值最上方带一句路由指引（问节点，别自己翻；整体变更交给父节点分发）。"""

import asyncio
import importlib.util
from pathlib import Path

TOOL_PATH = Path(__file__).resolve().parents[2] / 'tools' / 'task_progress_cn' / 'main' / 'tool.py'
_SPEC = importlib.util.spec_from_file_location('task_progress_cn_tool', TOOL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(MODULE)

HINT = MODULE.TREE_ROUTING_HINT
TASK_ID_PARAM = MODULE.TASK_ID_PARAM


class _Service:
    def __init__(self, *, text: str) -> None:
        self._text = text
        self.started = False

    async def startup(self) -> None:
        self.started = True

    def view_progress(self, task_id: str, *, mark_read: bool = True) -> str:
        return self._text


def _run(text: str) -> str:
    handler = MODULE._TaskProgressHandler(_Service(text=text))
    return asyncio.run(handler.execute(**{TASK_ID_PARAM: 'task:progress'}))


def test_tree_result_starts_with_the_routing_hint() -> None:
    result = _run('Task status: in_progress\n(node:root,in_progress,做素材)\n|-(node:c1,in_progress,渲染)')

    assert result.startswith(f'{HINT}\n')
    assert '直接发消息问它' in result
    assert '传给它们的父节点来处理' in result
    # 原结果整段留在指引之下，行序不变。
    assert result.endswith('Task status: in_progress\n(node:root,in_progress,做素材)\n|-(node:c1,in_progress,渲染)')


def test_unknown_task_returned_unchanged() -> None:
    text = 'Error: Task not found: task:progress'
    assert _run(text) == text


def test_status_line_without_any_node_returns_unchanged() -> None:
    # 指引说的对象是树里的节点；这台任务没给出任何节点行，就不印这句。
    text = 'Task status: in_progress'
    assert _run(text) == text
