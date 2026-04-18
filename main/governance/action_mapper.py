from __future__ import annotations

from typing import Any


DEFAULT_ALLOWED_ROLES = ['ceo', 'execution', 'inspection']
READ_ALLOWED_ROLES = ['ceo', 'execution', 'inspection']
WRITE_ALLOWED_ROLES = ['ceo', 'execution']

TOOL_FAMILY_ALIASES: dict[str, str] = {
    'filesystem_write': 'filesystem',
    'filesystem_edit': 'filesystem',
    'filesystem_copy': 'filesystem',
    'filesystem_move': 'filesystem',
    'filesystem_delete': 'filesystem',
    'filesystem_propose_patch': 'filesystem',
    'content_describe': 'content',
    'content_search': 'content',
    'content_open': 'content',
}


DEFAULT_TOOL_FAMILIES: dict[str, dict[str, Any]] = {
    'filesystem': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Write, edit, copy, move, delete, and propose patches for workspace files.',
        'actions': [
            {'id': 'write', 'label': 'Write File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': WRITE_ALLOWED_ROLES},
            {'id': 'edit', 'label': 'Edit File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': WRITE_ALLOWED_ROLES},
            {'id': 'copy', 'label': 'Copy Files', 'risk_level': 'high', 'destructive': True, 'allowed_roles': WRITE_ALLOWED_ROLES},
            {'id': 'move', 'label': 'Move Files', 'risk_level': 'high', 'destructive': True, 'allowed_roles': WRITE_ALLOWED_ROLES},
            {'id': 'delete', 'label': 'Delete File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': WRITE_ALLOWED_ROLES},
            {'id': 'propose_patch', 'label': 'Propose Patch', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': ['ceo', 'execution']},
        ],
    },
    'content': {
        'tool_id': 'content_navigation',
        'display_name': 'Content',
        'description': 'Describe, search, and open large externalized content by reference.',
        'actions': [{'id': 'inspect', 'label': 'Inspect Content', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES}],
    },
    'message': {
        'tool_id': 'messaging',
        'display_name': 'Messaging',
        'description': 'Send outbound user-facing messages.',
        'actions': [{'id': 'send', 'label': 'Send Message', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES}],
    },
    'cron': {
        'tool_id': 'automation',
        'display_name': 'Automation',
        'description': 'Schedule reminders and recurring jobs.',
        'actions': [{'id': 'schedule', 'label': 'Schedule Task', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': ['ceo', 'execution']}],
    },
    'exec': {
        'tool_id': 'exec_runtime',
        'display_name': 'Exec Runtime',
        'description': 'Execute shell commands.',
        'actions': [{'id': 'run', 'label': 'Run Command', 'risk_level': 'high', 'destructive': True, 'allowed_roles': ['ceo', 'execution']}],
    },
    'model_config': {
        'tool_id': 'model_admin',
        'display_name': 'Model Admin',
        'description': 'Manage model catalog and role routing.',
        'actions': [{'id': 'manage', 'label': 'Manage Models', 'risk_level': 'high', 'destructive': True, 'allowed_roles': ['ceo']}],
    },
    'memory_write': {
        'tool_id': 'memory',
        'display_name': 'Memory',
        'description': 'Write explicit permanent memory items.',
        'actions': [
            {
                'id': 'write',
                'label': 'Write Memory',
                'risk_level': 'medium',
                'destructive': False,
                'allowed_roles': ['ceo'],
            }
        ],
    },
    'memory_delete': {
        'tool_id': 'memory',
        'display_name': 'Memory',
        'description': 'Delete visible text from the committed MEMORY.md snapshot.',
        'actions': [
            {
                'id': 'delete',
                'label': 'Delete Memory',
                'risk_level': 'medium',
                'destructive': True,
                'allowed_roles': ['ceo'],
            }
        ],
    },
    'memory_note': {
        'tool_id': 'memory',
        'display_name': 'Memory',
        'description': 'Load a detailed memory note by short ref.',
        'actions': [
            {
                'id': 'note',
                'label': 'Load Memory Note',
                'risk_level': 'low',
                'destructive': False,
                'allowed_roles': ['ceo'],
            }
        ],
    },
    'memory_runtime': {
        'tool_id': 'memory',
        'display_name': 'Memory',
        'description': 'Expose queued markdown memory runtime internals.',
        'actions': [
            {
                'id': 'runtime',
                'label': 'Memory Runtime',
                'risk_level': 'low',
                'destructive': False,
                'agent_visible': False,
                'admin_mode': 'readonly_system',
                'allowed_roles': DEFAULT_ALLOWED_ROLES,
            }
        ],
    },
    'load_skill_context': {
        'tool_id': 'skill_access',
        'display_name': 'Skill Access',
        'description': 'Load detailed skill context for visible skills.',
        'actions': [{'id': 'load_context', 'label': 'Load Skill Context', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES}],
    },
    'load_tool_context': {
        'tool_id': 'skill_access',
        'display_name': 'Skill Access',
        'description': 'Load detailed tool usage context for visible tools.',
        'actions': [{'id': 'load_context', 'label': 'Load Tool Context', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES}],
    },
}


DEFAULT_FAMILY_ORDER = ['filesystem', 'content_navigation', 'memory', 'messaging', 'automation', 'exec_runtime', 'model_admin', 'task_runtime', 'skill_access']


def get_default_tool_governance(tool_name: str) -> dict[str, Any] | None:
    key = str(tool_name or '').strip()
    if not key:
        return None
    return DEFAULT_TOOL_FAMILIES.get(key) or DEFAULT_TOOL_FAMILIES.get(TOOL_FAMILY_ALIASES.get(key, ''))


def get_governance_tool_id(tool_name: str) -> str:
    payload = get_default_tool_governance(tool_name)
    if not isinstance(payload, dict):
        return ''
    return str(payload.get('tool_id') or '').strip()
