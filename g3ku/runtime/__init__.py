"""Runtime adapters, bridges, and session implementations."""

from g3ku.runtime.bootstrap_bridge import RuntimeBootstrapBridge
from g3ku.runtime.bridge import (
    SessionRuntimeBridge,
    SessionSubscription,
    build_state_snapshot,
    build_structured_event,
    cli_event_text,
)
from g3ku.runtime.channel_events import (
    build_channel_outbound_message,
    make_channel_event_listener,
    publish_channel_event,
)
from g3ku.runtime.manager import SessionRuntimeManager
from g3ku.runtime.message_adapter import (
    agent_message_to_dict,
    agent_messages_to_dicts,
    dict_to_agent_message,
    dicts_to_agent_messages,
)
from g3ku.runtime.model_bridge import LoopRuntimeContext, LoopRuntimeMiddleware, ModelExecutionBridge
from g3ku.runtime.engine import AgentRuntimeEngine
from g3ku.runtime.frontdoor import CeoExposureResolver, CeoFrontDoorRunner, CeoPromptBuilder
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.runtime.turns import RunTurnRequest, RunTurnResult
from g3ku.runtime.tool_bridge import ToolExecutionBridge

__all__ = [
    "AgentRuntimeEngine",
    "RuntimeBootstrapBridge",
    "agent_message_to_dict",
    "agent_messages_to_dicts",
    "dict_to_agent_message",
    "dicts_to_agent_messages",
    "LoopRuntimeContext",
    "RunTurnRequest",
    "RunTurnResult",
    "RuntimeAgentSession",
    "LoopRuntimeMiddleware",
    "ModelExecutionBridge",
    "SessionRuntimeBridge",
    "SessionRuntimeManager",
    "SessionSubscription",
    "build_channel_outbound_message",
    "CeoExposureResolver",
    "CeoFrontDoorRunner",
    "CeoPromptBuilder",
    "build_state_snapshot",
    "build_structured_event",
    "cli_event_text",
    "make_channel_event_listener",
    "publish_channel_event",
    "ToolExecutionBridge",
]

