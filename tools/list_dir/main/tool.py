from g3ku.agent.tools.filesystem import ListDirTool

def build(runtime):
    allowed_dir = runtime.workspace if getattr(runtime.loop, 'restrict_to_workspace', False) else None
    return ListDirTool(workspace=runtime.workspace, allowed_dir=allowed_dir)
