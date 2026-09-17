from g3ku.agent.tools.filesystem_stat import build_filesystem_stat_tool


def build(runtime):
    return build_filesystem_stat_tool(runtime)
