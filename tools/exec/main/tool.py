from g3ku.agent.tools.shell import ExecTool
from g3ku.resources.tool_settings import ExecToolSettings, runtime_tool_settings


def build(runtime):
    settings = runtime_tool_settings(runtime, ExecToolSettings, tool_name='exec')
    service = getattr(runtime.services, 'main_task_service', None)
    content_store = getattr(service, 'content_store', None) if service is not None else None
    return ExecTool(
        working_dir=None,
        workspace_root=str(runtime.workspace),
        timeout=settings.timeout,
        restrict_to_workspace=settings.restrict_to_workspace,
        path_append=settings.path_append,
        content_store=content_store,
        main_task_service=service,
    )
