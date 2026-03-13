from __future__ import annotations

from typing import Any


DEFAULT_ALLOWED_ROLES = ['ceo', 'execution', 'inspection']
READ_ALLOWED_ROLES = ['ceo', 'execution', 'inspection']
WRITE_ALLOWED_ROLES = ['ceo', 'execution']
INSPECTION_ALLOWED_ROLES = ['ceo', 'inspection']


DEFAULT_TOOL_FAMILIES: dict[str, dict[str, Any]] = {
    'read_file': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Read files from the workspace.',
        'actions': [{'id': 'read', 'label': 'Read File', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES}],
    },
    'list_dir': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'List workspace directories.',
        'actions': [{'id': 'list', 'label': 'List Directory', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES}],
    },
    'write_file': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Write files in the workspace.',
        'actions': [{'id': 'write', 'label': 'Write File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': WRITE_ALLOWED_ROLES}],
    },
    'edit_file': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Edit files in the workspace.',
        'actions': [{'id': 'edit', 'label': 'Edit File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': WRITE_ALLOWED_ROLES}],
    },
    'delete_file': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Delete files in the workspace.',
        'actions': [{'id': 'delete', 'label': 'Delete File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': ['ceo']}],
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
    'picture_washing': {
        'tool_id': 'media_tools',
        'display_name': 'Media Tools',
        'description': 'Generate or transform pictures.',
        'actions': [{'id': 'transform', 'label': 'Transform Picture', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES}],
    },
    'memory_search': {
        'tool_id': 'memory',
        'display_name': 'Memory',
        'description': 'Search long-term memory.',
        'actions': [{'id': 'search', 'label': 'Search Memory', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES}],
    },
    'file_vault_lookup': {
        'tool_id': 'file_vault',
        'display_name': 'File Vault',
        'description': 'Search uploaded vault content.',
        'actions': [{'id': 'lookup', 'label': 'Lookup Vault', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES}],
    },
    'file_vault_read': {
        'tool_id': 'file_vault',
        'display_name': 'File Vault',
        'description': 'Read uploaded vault content.',
        'actions': [{'id': 'read', 'label': 'Read Vault File', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES}],
    },
    'file_vault_stats': {
        'tool_id': 'file_vault',
        'display_name': 'File Vault',
        'description': 'Inspect vault status.',
        'actions': [{'id': 'stats', 'label': 'Vault Stats', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES}],
    },
    'file_vault_set_policy': {
        'tool_id': 'file_vault',
        'display_name': 'File Vault',
        'description': 'Change vault policy.',
        'actions': [{'id': 'set_policy', 'label': 'Set Vault Policy', 'risk_level': 'medium', 'destructive': True, 'allowed_roles': ['ceo']}],
    },
    'file_vault_cleanup': {
        'tool_id': 'file_vault',
        'display_name': 'File Vault',
        'description': 'Clean vault content.',
        'actions': [{'id': 'cleanup', 'label': 'Cleanup Vault', 'risk_level': 'medium', 'destructive': True, 'allowed_roles': ['ceo']}],
    },
    '创建异步任务': {
        'tool_id': 'task_runtime',
        'display_name': '?????',
        'description': 'Create background tasks in the main runtime.',
        'actions': [{'id': 'create_async_task', 'label': '??????', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': ['ceo']}],
    },
    '任务汇总工具': {
        'tool_id': 'task_runtime',
        'display_name': '?????',
        'description': 'Summarize background tasks.',
        'actions': [{'id': 'summary_cn', 'label': '????', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']}],
    },
    '获取任务': {
        'tool_id': 'task_runtime',
        'display_name': '?????',
        'description': 'List background tasks.',
        'actions': [{'id': 'list_cn', 'label': '??????', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']}],
    },
    '查看任务进度工具': {
        'tool_id': 'task_runtime',
        'display_name': '?????',
        'description': 'View task progress.',
        'actions': [{'id': 'progress_cn', 'label': '??????', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']}],
    },
    'load_skill_context': {
        'tool_id': 'skill_access',
        'display_name': 'Skill Access',
        'description': 'Load detailed skill context for visible skills.',
        'actions': [{'id': 'load_context', 'label': 'Load Skill Context', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo', 'execution', 'inspection']}],
    },
    'load_tool_context': {
        'tool_id': 'skill_access',
        'display_name': 'Skill Access',
        'description': 'Load detailed tool usage context for visible tools.',
        'actions': [{'id': 'load_context', 'label': 'Load Tool Context', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo', 'execution', 'inspection']}],
    },
    'propose_file_patch': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Create reviewable patch artifacts.',
        'actions': [{'id': 'propose_patch', 'label': 'Propose Patch', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': ['ceo', 'execution']}],
    },
}

DEFAULT_FAMILY_ORDER = ['filesystem', 'web_access', 'browser', 'memory', 'messaging', 'automation', 'exec_runtime', 'model_admin', 'media_tools', 'file_vault', 'task_runtime', 'skill_access']


def get_default_tool_governance(tool_name: str) -> dict[str, Any] | None:
    return DEFAULT_TOOL_FAMILIES.get(tool_name)
