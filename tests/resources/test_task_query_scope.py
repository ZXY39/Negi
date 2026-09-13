"""任务查询工具作用域（本会话/全局）单测。

覆盖：query_service.summary 口径标注与暂停细分、service.get_tasks 全局行格式、
内嵌 TaskSummaryTool/GetTasksTool 与资源工具 handler 的「查询范围」参数解析。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from main.models import TaskRecord
from main.service.runtime_service import MainRuntimeService

REPO_ROOT = Path(__file__).resolve().parents[2]


class _DummyChatBackend:
    async def chat(self, **kwargs):
        return SimpleNamespace(content='', tool_calls=[], finish_reason='stop', usage={})


def _make_web_service(tmp_path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )


def _task(task_id: str, session_id: str, status: str, *, is_paused: bool = False, is_unread: bool = False) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id=session_id,
        title=f'title-{task_id}',
        brief_text=f'brief-{task_id}',
        user_request='x',
        status=status,
        root_node_id='node-root',
        created_at='2026-09-13T10:00:00+08:00',
        updated_at='2026-09-13T10:00:00+08:00',
        is_paused=is_paused,
        is_unread=is_unread,
    )


def _seed(service: MainRuntimeService) -> None:
    service.store.upsert_task(_task('task:qq1', 'web:qq', 'success', is_unread=True))
    service.store.upsert_task(_task('task:qq2', 'web:qq', 'in_progress', is_paused=True))
    service.store.upsert_task(_task('task:web1', 'web:other', 'in_progress'))
    service.store.upsert_task(_task('task:web2', 'web:other', 'failed'))


def test_summary_session_vs_global_scope(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    _seed(service)

    session_text = service.summary('web:qq')
    assert 'session web:qq' in session_text
    assert '2 total' in session_text
    assert '1 in progress (1 paused)' in session_text

    global_text = service.summary(None)
    assert 'global' in global_text
    assert '4 total' in global_text
    assert '2 in progress (1 paused)' in global_text
    assert '1 failed' in global_text
    assert '1 unread' in global_text

    # 空串等价全局（工具层传 None；服务层兜底同口径）
    assert 'global' in service.summary('')


def test_get_tasks_global_rows_carry_status_and_session(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    _seed(service)

    session_list = service.get_tasks('web:qq', 1)
    assert '- task:qq1：brief-task:qq1' in session_list
    assert '(web:qq)' not in session_list  # 会话口径保持旧格式

    global_in_progress = service.get_tasks(None, 2)
    assert '[in_progress/paused] (web:qq)' in global_in_progress
    assert '[in_progress] (web:other)' in global_in_progress
    assert 'task:qq1' not in global_in_progress  # success 不在类型2

    assert service.get_tasks(None, 3) == '- task:web2 [failed] (web:other)：brief-task:web2'
    assert service.get_tasks('web:qq', 2) != '无匹配任务。'
    assert service.get_tasks('web:nobody', 2) == '无匹配任务。'


class _RecordingService:
    def __init__(self):
        self.summary_calls = []
        self.get_tasks_calls = []

    async def startup(self):
        return None

    def summary(self, session_id):
        self.summary_calls.append(session_id)
        return 'ok'

    def get_tasks(self, session_id, task_type):
        self.get_tasks_calls.append((session_id, task_type))
        return 'ok'


async def test_builtin_tools_scope_param() -> None:
    from main.service.runtime_service import GetTasksTool, TaskSummaryTool

    stub = _RecordingService()
    runtime = {'session_key': 'web:current'}

    summary_tool = TaskSummaryTool(stub)
    await summary_tool.execute(__g3ku_runtime=runtime)
    await summary_tool.execute(__g3ku_runtime=runtime, **{'查询范围': '全局'})
    await summary_tool.execute(__g3ku_runtime=runtime, **{'查询范围': '本会话'})
    assert stub.summary_calls == ['web:current', None, 'web:current']
    assert '查询范围' in summary_tool.parameters['properties']

    list_tool = GetTasksTool(stub)
    await list_tool.execute(__g3ku_runtime=runtime, **{'任务类型': 2})
    await list_tool.execute(__g3ku_runtime=runtime, **{'任务类型': 2, '查询范围': '全局'})
    assert stub.get_tasks_calls == [('web:current', 2), (None, 2)]
    assert '查询范围' in list_tool.parameters['properties']
    assert list_tool.parameters['required'] == ['任务类型']


def _load_resource_tool(rel_path: str):
    spec = importlib.util.spec_from_file_location(f'resource_tool_{rel_path.replace("/", "_")}', REPO_ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_resource_tools_scope_param() -> None:
    summary_mod = _load_resource_tool('tools/task_summary_cn/main/tool.py')
    fetch_mod = _load_resource_tool('tools/task_fetch_cn/main/tool.py')

    stub = _RecordingService()
    runtime = {'session_key': 'ext:qq-official:abc'}

    summary_handler = summary_mod.build(SimpleNamespace(services=SimpleNamespace(main_task_service=stub)))
    await summary_handler.execute(__g3ku_runtime=runtime)
    await summary_handler.execute(__g3ku_runtime=runtime, **{'查询范围': '全局'})
    assert stub.summary_calls == ['ext:qq-official:abc', None]
    assert '查询范围' in summary_handler.parameters['properties']

    list_handler = fetch_mod.build(SimpleNamespace(services=SimpleNamespace(main_task_service=stub)))
    await list_handler.execute(__g3ku_runtime=runtime, **{'任务类型': 1})
    await list_handler.execute(__g3ku_runtime=runtime, **{'任务类型': 1, '查询范围': '全局'})
    assert stub.get_tasks_calls == [('ext:qq-official:abc', 1), (None, 1)]
    assert '查询范围' in list_handler.parameters['properties']
