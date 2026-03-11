"""Builtin capability tool factories wired to the legacy AgentLoop runtime."""

from __future__ import annotations

from typing import Any

from g3ku.agent.tools.agent_browser import AgentBrowserTool
from g3ku.agent.tools.capabilities import (
    CapabilityDisableTool,
    CapabilityEnableTool,
    CapabilityInitTool,
    CapabilityInstallTool,
    CapabilityListTool,
    CapabilityRemoveTool,
    CapabilitySearchTool,
    CapabilitySourcesTool,
    CapabilityUpdateTool,
    CapabilityValidateTool,
)
from g3ku.agent.tools.cron import CronTool
from g3ku.agent.tools.file_vault import (
    FileVaultCleanupTool,
    FileVaultLookupTool,
    FileVaultReadTool,
    FileVaultSetPolicyTool,
    FileVaultStatsTool,
)
from g3ku.agent.tools.filesystem import DeleteFileTool, EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from g3ku.agent.tools.memory_search import MemorySearchTool
from g3ku.agent.tools.message import MessageTool
from g3ku.agent.tools.model_config import ModelConfigTool
from g3ku.agent.tools.picture_washing import PictureWashingTool
from g3ku.agent.tools.shell import ExecTool
from g3ku.agent.tools.web import WebFetchTool, WebSearchTool
from g3ku.capabilities.installer import CapabilityInstaller
from g3ku.capabilities.validator import CapabilityValidator


def _allowed_dir(loop: Any):
    return loop.workspace if loop.restrict_to_workspace else None


def build_read_file(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return ReadFileTool(workspace=loop.workspace, allowed_dir=_allowed_dir(loop))


def build_write_file(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return WriteFileTool(workspace=loop.workspace, allowed_dir=_allowed_dir(loop))


def build_edit_file(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return EditFileTool(workspace=loop.workspace, allowed_dir=_allowed_dir(loop))


def build_list_dir(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return ListDirTool(workspace=loop.workspace, allowed_dir=_allowed_dir(loop))


def build_delete_file(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return DeleteFileTool(workspace=loop.workspace, allowed_dir=_allowed_dir(loop))


def build_exec(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return ExecTool(
        working_dir=str(loop.workspace),
        timeout=loop.exec_config.timeout,
        restrict_to_workspace=loop.restrict_to_workspace,
        path_append=loop.exec_config.path_append,
    )


def build_web_search(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return WebSearchTool(api_key=loop.brave_api_key, proxy=loop.web_proxy)


def build_web_fetch(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return WebFetchTool(proxy=loop.web_proxy)


def build_picture_washing(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return PictureWashingTool(defaults=loop.picture_washing_config, agent_browser_defaults=loop.agent_browser_config)


def build_agent_browser(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return AgentBrowserTool(defaults=loop.agent_browser_config)


def build_message(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    return MessageTool(send_callback=loop.bus.publish_outbound)


def build_model_config(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor, loop
    return ModelConfigTool()


def build_cron(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    if not loop.cron_service:
        return None
    return CronTool(loop.cron_service)


def build_memory_search(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    if not loop.memory_manager or not loop._store_enabled:
        return None
    return MemorySearchTool(manager=loop.memory_manager, default_limit=loop.memory_config.retrieval.context_top_k if loop.memory_config else 8)


def build_file_vault_lookup(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    if loop.file_vault is None:
        return None
    return FileVaultLookupTool(vault=loop.file_vault)


def build_file_vault_read(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    if loop.file_vault is None:
        return None
    return FileVaultReadTool(vault=loop.file_vault)


def build_file_vault_stats(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    if loop.file_vault is None:
        return None
    return FileVaultStatsTool(vault=loop.file_vault)


def build_file_vault_set_policy(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    if loop.file_vault is None:
        return None
    return FileVaultSetPolicyTool(vault=loop.file_vault)


def build_file_vault_cleanup(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    if loop.file_vault is None:
        return None
    return FileVaultCleanupTool(vault=loop.file_vault)


def _capability_admin(loop: Any):
    registry = loop.capability_registry
    installer = loop.capability_installer or CapabilityInstaller(registry, source_registry=getattr(loop, "capability_source_registry", None), index_registry=getattr(loop, "capability_index_registry", None))
    validator = loop.capability_validator or CapabilityValidator(registry)
    loop.capability_installer = installer
    loop.capability_validator = validator
    return registry, installer, validator


def build_capability_list(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    registry, installer, validator = _capability_admin(loop)
    return CapabilityListTool(registry, installer, validator)


def build_capability_sources(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    registry, installer, validator = _capability_admin(loop)
    return CapabilitySourcesTool(registry, installer, validator)


def build_capability_search(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    registry, installer, validator = _capability_admin(loop)
    return CapabilitySearchTool(registry, installer, validator)


def build_capability_validate(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    registry, installer, validator = _capability_admin(loop)
    return CapabilityValidateTool(registry, installer, validator)


def build_capability_enable(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    registry, installer, validator = _capability_admin(loop)
    return CapabilityEnableTool(registry, installer, validator)


def build_capability_disable(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    registry, installer, validator = _capability_admin(loop)
    return CapabilityDisableTool(registry, installer, validator)


def build_capability_init(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    registry, installer, validator = _capability_admin(loop)
    return CapabilityInitTool(registry, installer, validator)


def build_capability_install(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    registry, installer, validator = _capability_admin(loop)
    return CapabilityInstallTool(registry, installer, validator)


def build_capability_update(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    registry, installer, validator = _capability_admin(loop)
    return CapabilityUpdateTool(registry, installer, validator)


def build_capability_remove(*, loop: Any, descriptor: Any | None = None):
    _ = descriptor
    registry, installer, validator = _capability_admin(loop)
    return CapabilityRemoveTool(registry, installer, validator)
