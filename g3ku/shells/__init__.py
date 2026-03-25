"""Shell entrypoint namespace for CLI and web surfaces."""
from g3ku.shells.cli import run_agent_shell
from g3ku.shells.memory_cli import build_memory_app
from g3ku.shells.provider_cli import build_provider_app
from g3ku.shells.resource_cli import build_resource_app
from g3ku.shells.web import debug_trace_enabled, get_agent, get_runtime_manager, run_web_shell

__all__ = [
    "build_memory_app",
    "build_provider_app",
    "build_resource_app",
    "debug_trace_enabled",
    "get_agent",
    "get_runtime_manager",
    "run_agent_shell",
    "run_web_shell",
]
