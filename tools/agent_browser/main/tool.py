from g3ku.agent.tools.agent_browser import AgentBrowserTool

def build(runtime):
    loop = runtime.loop
    return AgentBrowserTool(defaults=getattr(loop, 'agent_browser_config', None))
