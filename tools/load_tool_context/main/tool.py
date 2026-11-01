from pathlib import Path

import yaml

from g3ku.agent.tools.main_runtime import LoadToolContextTool

_MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / 'resource.yaml').read_text(encoding='utf-8'))


class PackLoadToolContextTool(LoadToolContextTool):
    """模型面那句只认 `resource.yaml`。

    包内类不再自撰一句：预算量的是清单，`tools[]` 发的也是清单，改一处即改到位。
    """

    @property
    def description(self) -> str:
        return str(_MANIFEST.get('description') or '')


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return PackLoadToolContextTool(lambda: getattr(runtime.services, 'main_task_service', service))
