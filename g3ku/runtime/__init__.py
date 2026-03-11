"""Runtime adapters, bridges, and session implementations."""

from g3ku.runtime.bootstrap_bridge import RuntimeBootstrapBridge
from g3ku.runtime.bridge import (
    SessionRuntimeBridge,
    SessionSubscription,
    build_state_snapshot,
    build_structured_event,
    cli_event_text,
    legacy_payloads_for_event,
)
from g3ku.runtime.channel_events import (
    build_channel_outbound_message,
    make_channel_event_listener,
    publish_channel_event,
)
from g3ku.runtime.context_pipeline import ContextTransformRequest, SessionContextPipeline
from g3ku.runtime.control_bridge import SessionControlBridge
from g3ku.runtime.manager import SessionRuntimeManager
from g3ku.runtime.message_adapter import (
    agent_message_to_dict,
    agent_messages_to_dicts,
    dict_to_agent_message,
    dicts_to_agent_messages,
)
from g3ku.runtime.multi_agent import (
    CompiledAgentRole,
    IntentGateDecision,
    MultiAgentRoleRegistry,
    MultiAgentRunner,
)
from g3ku.runtime.model_bridge import LoopRuntimeContext, LoopRuntimeMiddleware, ModelExecutionBridge
from g3ku.runtime.engine import AgentRuntimeEngine
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.runtime.session_runtime import LegacyAgentSession
from g3ku.runtime.turns import RunTurnRequest, RunTurnResult
from g3ku.runtime.session_services import SessionMemoryConsolidationService, SessionTranscriptService
from g3ku.runtime.tool_bridge import ToolExecutionBridge
from g3ku.runtime.turn_bridge import TurnLifecycleBridge

__all__ = [
    "AgentRuntimeEngine",
    "LegacyAgentSession",
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
    "CompiledAgentRole",
    "IntentGateDecision",
    "MultiAgentRoleRegistry",
    "MultiAgentRunner",
    "ModelExecutionBridge",
    "SessionMemoryConsolidationService",
    "SessionTranscriptService",
    "SessionRuntimeBridge",
    "SessionRuntimeManager",
    "SessionSubscription",
    "build_channel_outbound_message",
    "ContextTransformRequest",
    "SessionControlBridge",
    "build_state_snapshot",
    "build_structured_event",
    "cli_event_text",
    "legacy_payloads_for_event",
    "make_channel_event_listener",
    "publish_channel_event",
    "SessionContextPipeline",
    "ToolExecutionBridge",
    "TurnLifecycleBridge",
]

