from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from g3ku.agent.tools.base import Tool

TASK_ID_PARAM = '\u4efb\u52a1id'
TREE_ROUTING_HINT = (
    '\u60f3\u4e86\u89e3\u8282\u70b9\u5177\u4f53\u60c5\u51b5\u548c\u6709\u5173\u4fe1\u606f\uff0c\u76f4\u63a5\u53d1\u6d88\u606f\u95ee\u5b83\uff0c\u800c\u4e0d\u662f\u81ea\u5df1\u53bb\u7ffb\uff1b'
    '\u4fe1\u606f\u5982\u679c\u9700\u8981\u4f20\u9012\u591a\u4e2a\u8282\u70b9\u5e76\u4e14\u5b83\u4eec\u7684\u7236\u8282\u70b9\u9700\u8981\u77e5\u9053\uff08\u6bd4\u5982\u4efb\u52a1\u53d8\u66f4\u7b49\u6574\u4f53\u6539\u53d8\uff09\uff0c'
    '\u4f20\u7ed9\u5b83\u4eec\u7684\u7236\u8282\u70b9\u6765\u5904\u7406\uff0c\u8ba9\u5176\u5f80\u4e0b\u5206\u53d1\u3002'
)
_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


PROGRESS_TEXT_PREFIX = 'Task status:'


class _TaskProgressHandler(Tool):
    def __init__(self, service) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return str(_MANIFEST.get('name') or 'task_progress')

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(_MANIFEST.get('parameters') or {'type': 'object', 'properties': {}, 'required': []})

    async def execute(self, **kwargs: Any) -> str:
        await self._service.startup()
        task_id = str(kwargs.get(TASK_ID_PARAM) or '').strip()
        text = str(self._service.view_progress(task_id, mark_read=True) or '')
        # 指引说的对象是树里的节点：只有一行状态、没带任何节点结果时不印。
        if not text.startswith(PROGRESS_TEXT_PREFIX) or '\n' not in text:
            return text
        return f'{TREE_ROUTING_HINT}\n{text}'


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return _TaskProgressHandler(service)
