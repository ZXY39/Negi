from g3ku.agent.tools.memory_update import MemoryUpdateTool


def build(runtime):
    manager = getattr(runtime.services, "memory_manager", None)
    if manager is None:
        return None
    return MemoryUpdateTool(manager=manager)
