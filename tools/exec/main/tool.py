from g3ku.agent.tools.shell import ExecTool

def build(runtime):
    loop = runtime.loop
    exec_cfg = getattr(loop, 'exec_config', None)
    return ExecTool(working_dir=str(runtime.workspace), timeout=getattr(exec_cfg, 'timeout', 60), restrict_to_workspace=getattr(loop, 'restrict_to_workspace', False), path_append=getattr(exec_cfg, 'path_append', ''))
