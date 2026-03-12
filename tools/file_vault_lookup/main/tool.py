from g3ku.agent.tools.file_vault import FileVaultLookupTool

def build(runtime):
    vault = getattr(runtime.services, 'file_vault', None)
    if vault is None:
        return None
    return FileVaultLookupTool(vault=vault)
