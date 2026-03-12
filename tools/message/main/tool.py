from g3ku.agent.tools.message import MessageTool

def build(runtime):
    loop = runtime.loop
    callback = getattr(getattr(loop, 'bus', None), 'publish_outbound', None)
    if callback is None:
        return None
    return MessageTool(send_callback=callback)
