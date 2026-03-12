from g3ku.agent.tools.web import WebSearchTool

def build(runtime):
    loop = runtime.loop
    cfg = runtime.config_slice
    search_cfg = getattr(cfg, 'search', None)
    api_key = getattr(loop, 'brave_api_key', None) or getattr(search_cfg, 'api_key', '')
    max_results = getattr(search_cfg, 'max_results', 5)
    proxy = getattr(loop, 'web_proxy', None) or getattr(cfg, 'proxy', None)
    return WebSearchTool(api_key=api_key or None, max_results=max_results, proxy=proxy or None)
