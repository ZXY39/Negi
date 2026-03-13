from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from g3ku.agent.tools.base import Tool
from g3ku.org_graph.protocol import now_iso
from main.ids import new_node_id, new_task_id
from main.models import NodeRecord, TaskRecord
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.monitoring.query_service import TaskQueryService
from main.monitoring.tree_builder import TaskTreeBuilder
from main.runtime.chat_backend import ChatBackend
from main.runtime.node_runner import NodeRunner
from main.runtime.react_loop import ReActToolLoop
from main.runtime.task_runner import TaskRunner
from main.storage.sqlite_store import SQLiteTaskStore


class MainRuntimeService:
    def __init__(
        self,
        *,
        chat_backend: ChatBackend,
        store_path: Path | str | None = None,
        files_base_dir: Path | str | None = None,
        tool_provider: Callable[[NodeRecord], dict[str, Tool]] | None = None,
        execution_model_refs: list[str] | None = None,
        acceptance_model_refs: list[str] | None = None,
        default_max_depth: int = 1,
        hard_max_depth: int = 4,
        max_iterations: int = 16,
    ) -> None:
        self.store = SQLiteTaskStore(store_path or (Path.cwd() / '.g3ku' / 'main-runtime' / 'runtime.sqlite3'))
        self.file_store = TaskFileStore(files_base_dir or (Path.cwd() / '.g3ku' / 'main-runtime' / 'tasks'))
        self.tree_builder = TaskTreeBuilder()
        self.log_service = TaskLogService(store=self.store, file_store=self.file_store, tree_builder=self.tree_builder)
        self.query_service = TaskQueryService(store=self.store, file_store=self.file_store, log_service=self.log_service)
        self._tool_provider = tool_provider or (lambda _node: {})
        self._default_max_depth = max(0, int(default_max_depth or 0))
        self._hard_max_depth = max(self._default_max_depth, int(hard_max_depth or self._default_max_depth))
        react_loop = ReActToolLoop(chat_backend=chat_backend, log_service=self.log_service, max_iterations=max_iterations)
        self.node_runner = NodeRunner(
            store=self.store,
            log_service=self.log_service,
            react_loop=react_loop,
            tool_provider=self._tool_provider,
            execution_model_refs=list(execution_model_refs or ['execution']),
            acceptance_model_refs=list(acceptance_model_refs or execution_model_refs or ['inspection']),
        )
        self.task_runner = TaskRunner(store=self.store, log_service=self.log_service, node_runner=self.node_runner)
        self._started = False

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        for task in self.store.list_tasks():
            self.log_service.bootstrap_missing_files(task.task_id)
            if task.status != 'in_progress':
                continue
            runtime_state = self.log_service.read_runtime_state(task.task_id)
            if runtime_state is None:
                self.store.upsert_task(task.model_copy(update={'status': 'failed', 'failure_reason': 'runtime_state_corrupt', 'is_unread': True, 'updated_at': now_iso(), 'finished_at': now_iso()}))
                continue
            if bool(task.is_paused) or bool(runtime_state.get('paused')):
                continue
            self.task_runner.start_background(task.task_id)

    async def create_task(
        self,
        task: str,
        *,
        session_id: str = 'web:shared',
        max_depth: int | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        await self.startup()
        prompt = str(task or '').strip()
        if not prompt:
            raise ValueError('task must not be empty')
        effective_max_depth = self._clamp_depth(max_depth)
        task_id = new_task_id()
        root_node_id = new_node_id()
        now = now_iso()
        record = TaskRecord(
            task_id=task_id,
            session_id=str(session_id or 'web:shared').strip() or 'web:shared',
            title=(str(title or '').strip() or prompt[:60] or task_id),
            user_request=prompt,
            status='in_progress',
            root_node_id=root_node_id,
            max_depth=effective_max_depth,
            cancel_requested=False,
            pause_requested=False,
            is_paused=False,
            is_unread=True,
            brief_text='',
            created_at=now,
            updated_at=now,
            finished_at=None,
            final_output='',
            failure_reason='',
            metadata=dict(metadata or {}),
        )
        root = NodeRecord(
            node_id=root_node_id,
            task_id=task_id,
            parent_node_id=None,
            root_node_id=root_node_id,
            depth=0,
            node_kind='execution',
            status='in_progress',
            goal=prompt,
            prompt=prompt,
            input=prompt,
            output=[],
            check_result='',
            final_output='',
            can_spawn_children=0 < effective_max_depth,
            created_at=now,
            updated_at=now,
            metadata={},
        )
        record, root = self.log_service.initialize_task(record, root)
        self.task_runner.start_background(task_id)
        return self.store.get_task(record.task_id) or record

    async def cancel_task(self, task_id: str) -> TaskRecord | None:
        await self.task_runner.cancel(task_id)
        return self.get_task(task_id)

    async def pause_task(self, task_id: str) -> TaskRecord | None:
        await self.task_runner.pause(task_id)
        return self.get_task(task_id)

    async def resume_task(self, task_id: str) -> TaskRecord | None:
        await self.task_runner.resume(task_id)
        return self.get_task(task_id)

    async def wait_for_task(self, task_id: str) -> TaskRecord | None:
        await self.task_runner.wait(task_id)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self.store.get_task(task_id)

    def list_tasks(self, session_id: str | None = None) -> list[TaskRecord]:
        return self.store.list_tasks(session_id)

    def get_node(self, node_id: str) -> NodeRecord | None:
        return self.store.get_node(node_id)

    def list_nodes(self, task_id: str) -> list[NodeRecord]:
        return self.store.list_nodes(task_id)

    def summary(self, session_id: str) -> str:
        return self.query_service.summary(session_id).text

    def get_tasks(self, session_id: str, task_type: int) -> str:
        items = self.query_service.get_tasks(session_id, task_type)
        if not items:
            return '无匹配任务。'
        return '\n'.join(f'- {item.task_id}：{item.brief}' for item in items)

    def view_progress(self, task_id: str, *, mark_read: bool = True) -> str:
        payload = self.query_service.view_progress(task_id, mark_read=mark_read)
        if payload is None:
            return f'Error: Task not found: {task_id}'
        return payload.text

    async def close(self) -> None:
        await self.task_runner.close()
        self.store.close()

    def _clamp_depth(self, requested: int | None) -> int:
        if requested is None:
            return self._default_max_depth
        return max(0, min(int(requested), self._hard_max_depth))


class CreateAsyncTaskTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return '创建异步任务'

    @property
    def description(self) -> str:
        return '把用户需求转交为后台异步任务；主 agent 不可直接使用派生子节点。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {'task': {'type': 'string', 'description': '用户的原始需求。'}}, 'required': ['task']}

    async def execute(self, task: str, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        record = await self._service.create_task(str(task or ''), session_id=session_id)
        return f'创建任务成功{record.task_id}'


class TaskSummaryTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return '任务汇总工具'

    @property
    def description(self) -> str:
        return '返回总任务、进行中任务、失败任务数量。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {}}

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        await self._service.startup()
        return self._service.summary(str(runtime.get('session_key') or 'web:shared'))


class GetTasksTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return '获取任务'

    @property
    def description(self) -> str:
        return '按任务类型返回任务 id 列表和简要描述。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '任务类型': {'type': 'integer', 'enum': [1, 2, 3, 4], 'description': '1=所有任务，2=进行中任务，3=失败任务，4=未读任务。'},
            },
            'required': ['任务类型'],
        }

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        await self._service.startup()
        task_type = int(kwargs.get('任务类型'))
        return self._service.get_tasks(str(runtime.get('session_key') or 'web:shared'), task_type)


class ViewTaskProgressTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return '查看任务进度工具'

    @property
    def description(self) -> str:
        return '按任务 id 返回任务状态和树状图文本，并将任务标记为已读。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {'任务id': {'type': 'string', 'description': '目标任务 id。'}}, 'required': ['任务id']}

    async def execute(self, **kwargs: Any) -> str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        return self._service.view_progress(task_id, mark_read=True)
