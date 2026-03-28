from g3ku.agent.tools.memory_write import MemoryWriteTool


def build(runtime):
    manager = getattr(runtime.services, "memory_manager", None)
    if manager is None:
        return None
    return MemoryWriteTool(manager=manager)
