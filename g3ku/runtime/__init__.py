"""Runtime adapters, bridges, and session implementations.

This package intentionally avoids eager imports so submodules can be used without
pulling the full runtime bootstrap graph into import-time cycles.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    'RuntimeBootstrapBridge': ('g3ku.runtime.bootstrap_bridge', 'RuntimeBootstrapBridge'),
    'SessionRuntimeBridge': ('g3ku.runtime.bridge', 'SessionRuntimeBridge'),
    'SessionSubscription': ('g3ku.runtime.bridge', 'SessionSubscription'),
    'build_state_snapshot': ('g3ku.runtime.bridge', 'build_state_snapshot'),
    'build_structured_event': ('g3ku.runtime.bridge', 'build_structured_event'),
    'cli_event_text': ('g3ku.runtime.bridge', 'cli_event_text'),
    'build_channel_outbound_message': ('g3ku.runtime.channel_events', 'build_channel_outbound_message'),
    'make_channel_event_listener': ('g3ku.runtime.channel_events', 'make_channel_event_listener'),
    'publish_channel_event': ('g3ku.runtime.channel_events', 'publish_channel_event'),
    'SessionRuntimeManager': ('g3ku.runtime.manager', 'SessionRuntimeManager'),
    'agent_message_to_dict': ('g3ku.runtime.message_adapter', 'agent_message_to_dict'),
    'agent_messages_to_dicts': ('g3ku.runtime.message_adapter', 'agent_messages_to_dicts'),
    'dict_to_agent_message': ('g3ku.runtime.message_adapter', 'dict_to_agent_message'),
    'dicts_to_agent_messages': ('g3ku.runtime.message_adapter', 'dicts_to_agent_messages'),
    'LoopRuntimeContext': ('g3ku.runtime.model_bridge', 'LoopRuntimeContext'),
    'LoopRuntimeMiddleware': ('g3ku.runtime.model_bridge', 'LoopRuntimeMiddleware'),
    'ModelExecutionBridge': ('g3ku.runtime.model_bridge', 'ModelExecutionBridge'),
    'AgentRuntimeEngine': ('g3ku.runtime.engine', 'AgentRuntimeEngine'),
    'CeoExposureResolver': ('g3ku.runtime.frontdoor', 'CeoExposureResolver'),
    'CeoFrontDoorRunner': ('g3ku.runtime.frontdoor', 'CeoFrontDoorRunner'),
    'CeoPromptBuilder': ('g3ku.runtime.frontdoor', 'CeoPromptBuilder'),
    'RuntimeAgentSession': ('g3ku.runtime.session_agent', 'RuntimeAgentSession'),
    'RunTurnRequest': ('g3ku.runtime.turns', 'RunTurnRequest'),
    'RunTurnResult': ('g3ku.runtime.turns', 'RunTurnResult'),
    'ToolExecutionBridge': ('g3ku.runtime.tool_bridge', 'ToolExecutionBridge'),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
