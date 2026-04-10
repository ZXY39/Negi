from tools.content.main.tool import build_single_purpose_content_tool


def build(runtime):
    return build_single_purpose_content_tool(runtime, action='open')
