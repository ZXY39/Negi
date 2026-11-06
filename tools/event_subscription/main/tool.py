from g3ku.agent.tools.event_subscription import EventSubscriptionTool
from g3ku.runtime.event_subscriptions import EventSubscriptionManager


def build(runtime):
    service = getattr(runtime.services, "main_task_service", None)
    store = getattr(service, "store", None) if service is not None else None
    if store is None:
        return None
    return EventSubscriptionTool(EventSubscriptionManager(store, service))
