from g3ku.agent.tools.shell import ExecTool
from g3ku.resources.tool_settings import ExecToolSettings, runtime_tool_settings


def build(runtime):
    settings = runtime_tool_settings(runtime, ExecToolSettings, tool_name='exec')
    return ExecTool(
        working_dir=str(runtime.workspace),
        timeout=settings.timeout,
        restrict_to_workspace=settings.restrict_to_workspace,
        path_append=settings.path_append,
    )
