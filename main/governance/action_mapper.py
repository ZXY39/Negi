from __future__ import annotations

from typing import Any


DEFAULT_ALLOWED_ROLES = ['ceo', 'execution', 'inspection']
READ_ALLOWED_ROLES = ['ceo', 'execution', 'inspection']
WRITE_ALLOWED_ROLES = ['ceo', 'execution']


DEFAULT_TOOL_FAMILIES: dict[str, dict[str, Any]] = {
    'filesystem': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Read, list, write, edit, delete workspace files, and create reviewable patch artifacts.',
        'actions': [
            {'id': 'read', 'label': 'Read File', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES},
            {'id': 'list', 'label': 'List Directory', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES},
            {'id': 'write', 'label': 'Write File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': WRITE_ALLOWED_ROLES},
            {'id': 'edit', 'label': 'Edit File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': WRITE_ALLOWED_ROLES},
            {'id': 'delete', 'label': 'Delete File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': ['ceo']},
            {'id': 'propose_patch', 'label': 'Propose Patch', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': ['ceo', 'execution']},
        ],
    },
    'web_search': {
        'tool_id': 'web_access',
        'display_name': 'Web Access',
        'description': 'Search the web.',
        'actions': [{'id': 'search', 'label': 'Search Web', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES}],
    },
    'web_fetch': {
        'tool_id': 'web_access',
        'display_name': 'Web Access',
        'description': 'Fetch web content.',
        'actions': [{'id': 'fetch', 'label': 'Fetch Web', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES}],
    },
    'agent_browser': {
        'tool_id': 'browser',
        'display_name': 'Browser',
        'description': 'Interactive browser runtime.',
        'actions': [{'id': 'browse', 'label': 'Browser Runtime', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES}],
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
    'memory_search': {
        'tool_id': 'memory',
        'display_name': 'Memory',
        'description': 'Search long-term memory.',
        'actions': [{'id': 'search', 'label': 'Search Memory', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES}],
    },
    'create_async_task': {
        'tool_id': 'task_runtime',
        'display_name': 'Task Runtime',
        'description': 'Create background tasks in the main runtime.',
        'actions': [{'id': 'create_async_task', 'label': 'Create Async Task', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': ['ceo']}],
    },
    'task_summary': {
        'tool_id': 'task_runtime',
        'display_name': 'Task Runtime',
        'description': 'Summarize background tasks.',
        'actions': [{'id': 'summary_cn', 'label': 'Task Summary', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']}],
    },
    'task_list': {
        'tool_id': 'task_runtime',
        'display_name': 'Task Runtime',
        'description': 'List background tasks.',
        'actions': [{'id': 'list_cn', 'label': 'Task List', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']}],
    },
    'task_progress': {
        'tool_id': 'task_runtime',
        'display_name': 'Task Runtime',
        'description': 'View task progress.',
        'actions': [{'id': 'progress_cn', 'label': 'Task Progress', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']}],
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


DEFAULT_FAMILY_ORDER = ['filesystem', 'web_access', 'browser', 'memory', 'messaging', 'automation', 'exec_runtime', 'model_admin', 'task_runtime', 'skill_access']


def get_default_tool_governance(tool_name: str) -> dict[str, Any] | None:
    return DEFAULT_TOOL_FAMILIES.get(tool_name)
