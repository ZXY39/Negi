from g3ku.agent.tools.orggraph import LoadSkillContextTool

def build(runtime):
    service = getattr(runtime.services, 'org_graph_service', None)
    if service is None:
        return None
    return LoadSkillContextTool(lambda: getattr(runtime.services, 'org_graph_service', service))
