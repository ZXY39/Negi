from g3ku.agent.tools.cron import CronTool

def build(runtime):
    service = getattr(runtime.services, 'cron_service', None)
    if service is None:
        return None
    return CronTool(service)
