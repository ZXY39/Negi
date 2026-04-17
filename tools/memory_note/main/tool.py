from g3ku.agent.tools.memory_note import MemoryNoteTool


def build(runtime):
    manager = getattr(runtime.services, "memory_manager", None)
    if manager is None:
        return None
    return MemoryNoteTool(manager=manager)
