from main.service.runtime_service import ViewTaskProgressTool


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return ViewTaskProgressTool(service)
