"""Shell entrypoint namespace for CLI, web, and gateway surfaces."""

from g3ku.shells.capability_cli import build_capability_app
from g3ku.shells.channels_cli import build_channels_app
from g3ku.shells.cli import run_agent_shell
from g3ku.shells.gateway import run_gateway_shell
from g3ku.shells.memory_cli import build_memory_app
from g3ku.shells.provider_cli import build_provider_app
from g3ku.shells.web import debug_trace_enabled, get_agent, get_runtime_manager, run_web_shell

__all__ = [
    "build_capability_app",
    "build_channels_app",
    "build_memory_app",
    "build_provider_app",
    "debug_trace_enabled",
    "get_agent",
    "get_runtime_manager",
    "run_agent_shell",
    "run_gateway_shell",
    "run_web_shell",
]
