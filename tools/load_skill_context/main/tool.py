from g3ku.agent.tools.main_runtime import LoadSkillContextTool

def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    if service is None:
        return None
    return LoadSkillContextTool(lambda: getattr(runtime.services, 'main_task_service', service))
