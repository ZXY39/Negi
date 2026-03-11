from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from g3ku.org_graph.types import PermissionRiskLevel

DEFAULT_ALLOWED_ROLES = ['ceo', 'execution', 'inspection']
READ_ALLOWED_ROLES = ['ceo', 'execution', 'inspection']
MUTATION_ALLOWED_ROLES = ['ceo', 'execution']
INSPECTION_ALLOWED_ROLES = ['ceo', 'inspection']
CEO_ONLY_ROLES = ['ceo']


@dataclass(slots=True)
class MappedToolAction:
    tool_id: str
    action_id: str
    label: str
    risk_level: PermissionRiskLevel
    destructive: bool
    allowed_roles: list[str]


DEFAULT_TOOL_FAMILIES: dict[str, dict[str, Any]] = {
    'read_file': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Workspace file and directory operations.',
        'actions': [
            {'id': 'read', 'label': 'Read File', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES},
        ],
    },
    'list_dir': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Workspace file and directory operations.',
        'actions': [
            {'id': 'list', 'label': 'List Directory', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES},
        ],
    },
    'write_file': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Workspace file and directory operations.',
        'actions': [
            {
                'id': 'create',
                'label': 'Create File',
                'risk_level': 'medium',
                'destructive': True,
                'allowed_roles': MUTATION_ALLOWED_ROLES,
                'match': {'kind': 'filesystem_existence', 'path_param': 'path', 'when': 'missing'},
            },
            {
                'id': 'write',
                'label': 'Overwrite File',
                'risk_level': 'high',
                'destructive': True,
                'allowed_roles': CEO_ONLY_ROLES,
                'match': {'kind': 'filesystem_existence', 'path_param': 'path', 'when': 'exists'},
            },
        ],
    },
    'edit_file': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Workspace file and directory operations.',
        'actions': [
            {'id': 'edit', 'label': 'Edit File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': CEO_ONLY_ROLES},
        ],
    },
    'delete_file': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Workspace file and directory operations.',
        'actions': [
            {'id': 'delete', 'label': 'Delete File', 'risk_level': 'high', 'destructive': True, 'allowed_roles': CEO_ONLY_ROLES},
        ],
    },
    'propose_file_patch': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Workspace file and directory operations.',
        'actions': [
            {'id': 'patch_propose', 'label': 'Propose Patch', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': MUTATION_ALLOWED_ROLES},
        ],
    },
    'patch_apply': {
        'tool_id': 'filesystem',
        'display_name': 'Filesystem',
        'description': 'Workspace file and directory operations.',
        'actions': [
            {'id': 'patch_apply', 'label': 'Apply Patch', 'risk_level': 'high', 'destructive': True, 'allowed_roles': CEO_ONLY_ROLES},
        ],
    },
    'web_search': {
        'tool_id': 'web_access',
        'display_name': 'Web Access',
        'description': 'Open web search and fetch capabilities.',
        'actions': [
            {'id': 'search', 'label': 'Web Search', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES},
        ],
    },
    'web_fetch': {
        'tool_id': 'web_access',
        'display_name': 'Web Access',
        'description': 'Open web search and fetch capabilities.',
        'actions': [
            {'id': 'fetch', 'label': 'Web Fetch', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES},
        ],
    },
    'memory_search': {
        'tool_id': 'memory',
        'display_name': 'Memory',
        'description': 'Search structured memory context.',
        'actions': [
            {'id': 'search', 'label': 'Memory Search', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES},
        ],
    },
    'agent_browser': {
        'tool_id': 'browser',
        'display_name': 'Browser',
        'description': 'Interactive browser automation and inspection.',
        'actions': [
            {'id': 'open', 'label': 'Open Page', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES, 'match': {'kind': 'browser_command', 'command': 'open'}},
            {'id': 'snapshot', 'label': 'Snapshot DOM', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES, 'match': {'kind': 'browser_command', 'command': 'snapshot'}},
            {'id': 'click', 'label': 'Click Element', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': MUTATION_ALLOWED_ROLES, 'match': {'kind': 'browser_command', 'command': 'click'}},
            {'id': 'fill', 'label': 'Fill Form', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': MUTATION_ALLOWED_ROLES, 'match': {'kind': 'browser_command', 'command': 'fill'}},
            {'id': 'get', 'label': 'Read State', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES, 'match': {'kind': 'browser_command', 'command': 'get'}},
            {'id': 'wait', 'label': 'Wait', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES, 'match': {'kind': 'browser_command', 'command': 'wait'}},
            {'id': 'cookies', 'label': 'Cookies', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': INSPECTION_ALLOWED_ROLES, 'match': {'kind': 'browser_command', 'command': 'cookies'}},
            {'id': 'storage', 'label': 'Storage', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': INSPECTION_ALLOWED_ROLES, 'match': {'kind': 'browser_command', 'command': 'storage'}},
            {'id': 'screenshot', 'label': 'Screenshot', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES, 'match': {'kind': 'browser_command', 'command': 'screenshot'}},
            {'id': 'close', 'label': 'Close Browser', 'risk_level': 'low', 'destructive': False, 'allowed_roles': READ_ALLOWED_ROLES, 'match': {'kind': 'browser_command', 'command': 'close'}},
        ],
    },
    'message': {
        'tool_id': 'messaging',
        'display_name': 'Messaging',
        'description': 'Send outbound user-facing messages.',
        'actions': [
            {'id': 'send', 'label': 'Send Message', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES},
        ],
    },
    'cron': {
        'tool_id': 'automation',
        'display_name': 'Automation',
        'description': 'Schedule reminders and recurring jobs.',
        'actions': [
            {'id': 'schedule', 'label': 'Schedule Task', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': ['ceo', 'execution']},
        ],
    },
    'exec': {
        'tool_id': 'exec_runtime',
        'display_name': 'Exec Runtime',
        'description': 'Execute shell commands.',
        'actions': [
            {'id': 'run', 'label': 'Run Command', 'risk_level': 'high', 'destructive': True, 'allowed_roles': ['ceo', 'execution']},
        ],
    },
    'model_config': {
        'tool_id': 'model_admin',
        'display_name': 'Model Admin',
        'description': 'Manage model catalog and role routing.',
        'actions': [
            {'id': 'manage', 'label': 'Manage Models', 'risk_level': 'high', 'destructive': True, 'allowed_roles': ['ceo']},
        ],
    },
    'picture_washing': {
        'tool_id': 'media_tools',
        'display_name': 'Media Tools',
        'description': 'Generate or transform pictures.',
        'actions': [
            {'id': 'transform', 'label': 'Transform Picture', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES},
        ],
    },
    'file_vault_lookup': {
        'tool_id': 'file_vault',
        'display_name': 'File Vault',
        'description': 'Search and manage uploaded file vault content.',
        'actions': [
            {'id': 'lookup', 'label': 'Lookup Vault', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES},
        ],
    },
    'file_vault_read': {
        'tool_id': 'file_vault',
        'display_name': 'File Vault',
        'description': 'Search and manage uploaded file vault content.',
        'actions': [
            {'id': 'read', 'label': 'Read Vault File', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES},
        ],
    },
    'file_vault_stats': {
        'tool_id': 'file_vault',
        'display_name': 'File Vault',
        'description': 'Search and manage uploaded file vault content.',
        'actions': [
            {'id': 'stats', 'label': 'Vault Stats', 'risk_level': 'low', 'destructive': False, 'allowed_roles': DEFAULT_ALLOWED_ROLES},
        ],
    },
    'file_vault_set_policy': {
        'tool_id': 'file_vault',
        'display_name': 'File Vault',
        'description': 'Search and manage uploaded file vault content.',
        'actions': [
            {'id': 'set_policy', 'label': 'Set Vault Policy', 'risk_level': 'medium', 'destructive': True, 'allowed_roles': ['ceo']},
        ],
    },
    'file_vault_cleanup': {
        'tool_id': 'file_vault',
        'display_name': 'File Vault',
        'description': 'Search and manage uploaded file vault content.',
        'actions': [
            {'id': 'cleanup', 'label': 'Cleanup Vault', 'risk_level': 'medium', 'destructive': True, 'allowed_roles': ['ceo']},
        ],
    },
    'orggraph_create_project': {
        'tool_id': 'project_execution',
        'display_name': 'Project Execution',
        'description': 'Create and control org-graph background projects.',
        'actions': [
            {'id': 'create_project', 'label': 'Create Project', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': ['ceo']},
        ],
    },
    'orggraph_control_project': {
        'tool_id': 'project_execution',
        'display_name': 'Project Execution',
        'description': 'Create and control org-graph background projects.',
        'actions': [
            {'id': 'control_project', 'label': 'Control Project', 'risk_level': 'medium', 'destructive': False, 'allowed_roles': ['ceo']},
        ],
    },
    'task_monitor_summary': {
        'tool_id': 'task_monitoring',
        'display_name': 'Task Monitoring',
        'description': 'Summarize and inspect background project progress.',
        'actions': [
            {'id': 'summary', 'label': 'Task Summary', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']},
        ],
    },
    'task_monitor_list': {
        'tool_id': 'task_monitoring',
        'display_name': 'Task Monitoring',
        'description': 'Summarize and inspect background project progress.',
        'actions': [
            {'id': 'list', 'label': 'Task List', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']},
        ],
    },
    'task_monitor_progress': {
        'tool_id': 'task_monitoring',
        'display_name': 'Task Monitoring',
        'description': 'Summarize and inspect background project progress.',
        'actions': [
            {'id': 'progress', 'label': 'Task Progress', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']},
        ],
    },
    'task_monitor_engineering_exceptions': {
        'tool_id': 'task_monitoring',
        'display_name': 'Task Monitoring',
        'description': 'Summarize and inspect background project progress.',
        'actions': [
            {'id': 'engineering_exceptions', 'label': 'Engineering Exceptions', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']},
        ],
    },
    'load_skill_context': {
        'tool_id': 'skill_access',
        'display_name': 'Skill Access',
        'description': 'Load detailed skill context for currently visible skills.',
        'actions': [
            {'id': 'load_context', 'label': 'Load Skill Context', 'risk_level': 'low', 'destructive': False, 'allowed_roles': ['ceo']},
        ],
    },
}


DEFAULT_FAMILY_ORDER = ['filesystem', 'web_access', 'browser', 'memory', 'messaging', 'automation', 'exec_runtime', 'model_admin', 'media_tools', 'file_vault', 'project_execution', 'task_monitoring', 'skill_access']


def get_default_tool_governance(tool_name: str) -> dict[str, Any] | None:
    return DEFAULT_TOOL_FAMILIES.get(tool_name)


def resolve_tool_action(tool_name: str, arguments: dict[str, Any] | None = None, workspace: Path | None = None) -> MappedToolAction | None:
    governance = get_default_tool_governance(tool_name)
    if governance is None:
        return None
    args = dict(arguments or {})
    actions = list(governance.get('actions') or [])
    for action in actions:
        if _matches_action(action, args, workspace=workspace):
            return MappedToolAction(
                tool_id=str(governance['tool_id']),
                action_id=str(action['id']),
                label=str(action['label']),
                risk_level=str(action['risk_level']),
                destructive=bool(action.get('destructive', False)),
                allowed_roles=list(action.get('allowed_roles') or DEFAULT_ALLOWED_ROLES),
            )
    if actions:
        action = actions[0]
        return MappedToolAction(
            tool_id=str(governance['tool_id']),
            action_id=str(action['id']),
            label=str(action['label']),
            risk_level=str(action['risk_level']),
            destructive=bool(action.get('destructive', False)),
            allowed_roles=list(action.get('allowed_roles') or DEFAULT_ALLOWED_ROLES),
        )
    return None


def _matches_action(action: dict[str, Any], arguments: dict[str, Any], *, workspace: Path | None) -> bool:
    match = dict(action.get('match') or {})
    kind = str(match.get('kind') or 'static')
    if kind == 'static':
        return True
    if kind == 'browser_command':
        return str(arguments.get('command') or '').strip().lower() == str(match.get('command') or '').strip().lower()
    if kind == 'filesystem_existence':
        raw_path = str(arguments.get(str(match.get('path_param') or 'path')) or '').strip()
        if not raw_path:
            return False
        path = Path(raw_path).expanduser()
        if workspace is not None and not path.is_absolute():
            path = workspace / path
        exists = path.exists()
        when = str(match.get('when') or 'exists').strip().lower()
        if when == 'exists':
            return exists
        if when == 'missing':
            return not exists
    return False

