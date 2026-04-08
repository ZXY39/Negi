from g3ku.agent.tools.memory_delete import MemoryDeleteTool


def build(runtime):
    manager = getattr(runtime.services, "memory_manager", None)
    if manager is None:
        return None
    return MemoryDeleteTool(manager=manager)

