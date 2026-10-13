from __future__ import annotations

from typing import Any


DEFAULT_ALLOWED_ROLES = ['ceo', 'execution', 'inspection']
READ_ALLOWED_ROLES = ['ceo', 'execution', 'inspection']
WRITE_ALLOWED_ROLES = ['ceo', 'execution']


# 默认治理表按工具名索引：一个工具一条，没有中间层。
# 只有显式声明 governance 或列在这张表里的工具才会进入管理面，
# 两者都没有的工具（如 stop_tool_execution）按设计不进 exposure 名单。
DEFAULT_TOOL_GOVERNANCE: dict[str, dict[str, Any]] = {
    'cron': {
        'display_name': 'Cron Schedule',
        'description': 'Schedule reminders and recurring jobs.',
        'risk_level': 'medium',
        'destructive': False,
        'allowed_roles': WRITE_ALLOWED_ROLES,
    },
    'exec': {
        'display_name': 'Exec Runtime',
        'description': 'Execute shell commands.',
        'risk_level': 'high',
        'destructive': True,
        'allowed_roles': WRITE_ALLOWED_ROLES,
    },
    'model_config': {
        'display_name': 'Manage Models',
        'description': 'Manage model catalog and role routing.',
        'risk_level': 'high',
        'destructive': True,
        'allowed_roles': ['ceo'],
    },
    'memory_update': {
        'display_name': 'Update Memory',
        'description': 'Queue a natural-language request to remember, revise, or forget long-term memory.',
        'risk_level': 'medium',
        'destructive': True,
        'allowed_roles': ['ceo'],
    },
    'memory_note': {
        'display_name': 'Load Memory Note',
        'description': 'Load a detailed memory note by short ref.',
        'risk_level': 'low',
        'destructive': False,
        'allowed_roles': ['ceo'],
    },
    'memory_runtime': {
        'display_name': 'Memory Runtime',
        'description': 'Expose queued markdown memory runtime internals.',
        'risk_level': 'low',
        'destructive': False,
        'agent_visible': False,
        'admin_mode': 'readonly_system',
        'allowed_roles': DEFAULT_ALLOWED_ROLES,
    },
    'load_skill_context': {
        'display_name': 'Load Skill Context',
        'description': 'Load detailed skill context for visible skills.',
        'risk_level': 'low',
        'destructive': False,
        'allowed_roles': DEFAULT_ALLOWED_ROLES,
    },
    'load_tool_context': {
        'display_name': 'Load Tool Context',
        'description': 'Load detailed tool usage context for visible tools.',
        'risk_level': 'low',
        'destructive': False,
        'allowed_roles': DEFAULT_ALLOWED_ROLES,
    },
}


def get_default_tool_governance(tool_name: str) -> dict[str, Any] | None:
    key = str(tool_name or '').strip()
    if not key:
        return None
    return DEFAULT_TOOL_GOVERNANCE.get(key)
