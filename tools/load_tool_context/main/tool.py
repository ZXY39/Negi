from g3ku.agent.tools.main_runtime import LoadToolContextTool

def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return LoadToolContextTool(lambda: getattr(runtime.services, 'main_task_service', service))
