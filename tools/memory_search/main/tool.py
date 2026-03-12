from g3ku.agent.tools.memory_search import MemorySearchTool

def build(runtime):
    manager = getattr(runtime.services, 'memory_manager', None)
    loop = runtime.loop
    if manager is None or not getattr(loop, '_store_enabled', False):
        return None
    cfg = getattr(loop, 'memory_config', None)
    retrieval = getattr(cfg, 'retrieval', None)
    return MemorySearchTool(manager=manager, default_limit=getattr(retrieval, 'context_top_k', 8))
