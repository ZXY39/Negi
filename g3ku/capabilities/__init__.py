"""Capability pack platform for tools, skills, and agent roles."""

from g3ku.capabilities.index_registry import CapabilityIndexRegistry
from g3ku.capabilities.installer import CapabilityInstaller
from g3ku.capabilities.loader import CapabilityLoader
from g3ku.capabilities.models import AgentRoleDescriptor
from g3ku.capabilities.registry import CapabilityRegistry
from g3ku.capabilities.state_store import CapabilityStateStore
from g3ku.capabilities.validator import CapabilityValidator

CAPABILITY_API_VERSION = "1.0"

__all__ = [
    "AgentRoleDescriptor",
    "CAPABILITY_API_VERSION",
    "CapabilityIndexRegistry",
    "CapabilityInstaller",
    "CapabilityLoader",
    "CapabilityRegistry",
    "CapabilityStateStore",
    "CapabilityValidator",
]
