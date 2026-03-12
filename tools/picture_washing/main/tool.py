from g3ku.agent.tools.picture_washing import PictureWashingTool

def build(runtime):
    loop = runtime.loop
    return PictureWashingTool(defaults=getattr(loop, 'picture_washing_config', None), agent_browser_defaults=getattr(loop, 'agent_browser_config', None))
