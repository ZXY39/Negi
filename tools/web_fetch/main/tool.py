from g3ku.agent.tools.web import WebFetchTool

def build(runtime):
    loop = runtime.loop
    cfg = runtime.config_slice
    proxy = getattr(loop, 'web_proxy', None) or getattr(cfg, 'proxy', None)
    return WebFetchTool(proxy=proxy or None)
