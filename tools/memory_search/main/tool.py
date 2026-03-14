from g3ku.agent.tools.memory_search import MemorySearchTool
from g3ku.resources.tool_settings import MemorySearchToolSettings, runtime_tool_settings


def build(runtime):
    manager = getattr(runtime.services, 'memory_manager', None)
    loop = runtime.loop
    if manager is None or not getattr(loop, '_store_enabled', False):
        return None
    settings = runtime_tool_settings(runtime, MemorySearchToolSettings, tool_name='memory_search')
    return MemorySearchTool(manager=manager, default_limit=settings.default_limit)
