from g3ku.agent.tools.filesystem_mutation import build_single_purpose_filesystem_tool


def build(runtime):
    return build_single_purpose_filesystem_tool(runtime, action='copy')
