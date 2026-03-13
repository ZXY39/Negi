from g3ku.agent.tools.propose_patch import ProposeFilePatchTool


def build(runtime):
    service = getattr(runtime.services, 'main_task_service', None)
    artifact_store = getattr(service, 'artifact_store', None) if service is not None else None
    if artifact_store is None:
        return None
    allowed_dir = runtime.workspace if getattr(runtime.loop, 'restrict_to_workspace', False) else None
    return ProposeFilePatchTool(
        artifact_store=artifact_store,
        workspace=runtime.workspace,
        allowed_dir=allowed_dir,
    )
