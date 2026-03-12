from g3ku.agent.tools.orggraph import OrgGraphCreateProjectTool

def build(runtime):
    service = getattr(runtime.services, 'org_graph_service', None)
    if service is None:
        return None
    return OrgGraphCreateProjectTool(lambda: getattr(runtime.services, 'org_graph_service', service))
