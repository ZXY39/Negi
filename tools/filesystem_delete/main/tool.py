from tools.filesystem.main.tool import build_single_purpose_filesystem_tool


def build(runtime):
    return build_single_purpose_filesystem_tool(runtime, action='delete')
