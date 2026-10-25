"""节点预置契约这条车道：过滤判据、消息形状、bootstrap 落位与观测落点。

用例从**车道进口**喂（`create_task` / `_build_messages` / `_create_execution_child`），
不直接喂渲染器：显示与准入都要在路上逐层活下来，只测渲染器会漏掉挂错位置的指针。
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from g3ku.resources import ResourceManager
from g3ku.runtime.tool_contract_block import contract_pairs_from_value, render_tool_contract
from main.models import SpawnChildResult, SpawnChildSpec
from main.protocol import now_iso
from main.runtime.node_prompt_contract import (
    NODE_DYNAMIC_CONTRACT_HEADING,
)
from main.runtime.node_seed_contracts import (
    NODE_SEED_CONTRACT_SINGLE_CHAR_LIMIT,
    NODE_SEED_CONTRACT_TOTAL_CHAR_LIMIT,
)
from main.service.runtime_service import MainRuntimeService

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKER_PREFIX = '[TOOL_CONTRACT:'


class _DummyChatBackend:
    async def chat(self, **kwargs: Any) -> Any:
        raise AssertionError(f'chat backend should not be called in this test: {kwargs!r}')


def _resource_app_config() -> SimpleNamespace:
    return SimpleNamespace(
        resources=SimpleNamespace(
            enabled=True,
            skills_dir='skills',
            tools_dir='tools',
            manifest_name='resource.yaml',
            state_path='.g3ku/resources.state.json',
            reload=SimpleNamespace(
                enabled=False,
                poll_interval_ms=10000,
                debounce_ms=100,
                lazy_reload_on_access=False,
                keep_last_good_version=True,
            ),
            locks=SimpleNamespace(
                lock_dir='.g3ku/resource-locks',
                logical_delete_guard=True,
                windows_fs_lock=True,
            ),
        )
    )


_MANIFEST = textwrap.dedent(
    """\
    schema_version: 1
    kind: tool
    name: {name}
    description: Seed lane demo tool.
    tool_result_delivery_contract: runtime_managed
    protocol: mcp
    mcp:
      transport: embedded
    requires:
      tools: []
      bins: []
      paths: {paths}
      env: []
    permissions:
      network: false
      filesystem: []
    parameters:
      type: object
      properties:
        value:
          type: string
          description: Demo argument.
      required: []
    governance:
      display_name: {name}
      description: Seed lane demo tool.
      risk_level: low
      destructive: false
      allowed_roles:
    {roles}
    exposure:
      agent: true
      main_runtime: true
    toolskill:
      enabled: true
    """
)

_HANDLER = textwrap.dedent(
    """\
    from __future__ import annotations

    import json
    from typing import Any

    from g3ku.agent.tools.base import Tool


    class _DemoTool(Tool):
        @property
        def name(self) -> str:
            return '{name}'

        @property
        def description(self) -> str:
            return 'Seed lane demo tool.'

        @property
        def parameters(self) -> dict[str, Any]:
            return {{'type': 'object', 'properties': {{'value': {{'type': 'string'}}}}, 'required': []}}

        async def execute(self, value: str = '', **kwargs: Any) -> str:
            return json.dumps({{'ok': True, 'tool': '{name}', 'value': value}}, ensure_ascii=False)


    def build(runtime):
        return _DemoTool()
    """
)


def _write_tool(
    workspace: Path,
    name: str,
    *,
    roles: tuple[str, ...] = ('ceo', 'execution', 'inspection'),
    toolskill_body: str | None = '# demo\n\nSeed lane body.\n',
    missing_path: str = '',
) -> None:
    root = workspace / 'tools' / name
    (root / 'main').mkdir(parents=True, exist_ok=True)
    (root / 'resource.yaml').write_text(
        _MANIFEST.format(
            name=name,
            paths=f'[{missing_path}]' if missing_path else '[]',
            roles='\n'.join(f'    - {role}' for role in roles),
        ),
        encoding='utf-8',
    )
    (root / 'main' / 'tool.py').write_text(_HANDLER.format(name=name), encoding='utf-8')
    if toolskill_body is not None:
        (root / 'toolskills').mkdir(parents=True, exist_ok=True)
        (root / 'toolskills' / 'SKILL.md').write_text(toolskill_body, encoding='utf-8')


def _build_service(tmp_path: Path) -> tuple[MainRuntimeService, ResourceManager]:
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _write_tool(workspace, 'seed_a')
    _write_tool(workspace, 'seed_b')
    _write_tool(workspace, 'seed_ceo_only', roles=('ceo',))
    _write_tool(workspace, 'seed_repair', missing_path='externaltools/seed_repair/runtime.ok')
    _write_tool(workspace, 'seed_no_skill', toolskill_body=None)
    _write_tool(
        workspace,
        'seed_fat',
        toolskill_body='# fat\n\n' + ('x' * (NODE_SEED_CONTRACT_SINGLE_CHAR_LIMIT + 500)) + '\n',
    )
    _write_tool(workspace, 'seed_wide_1', toolskill_body='# wide1\n\n' + ('y' * 13000) + '\n')
    _write_tool(workspace, 'seed_wide_2', toolskill_body='# wide2\n\n' + ('z' * 13000) + '\n')
    shutil.copytree(
        REPO_ROOT / 'tools' / 'exec',
        workspace / 'tools' / 'exec',
        ignore=shutil.ignore_patterns('__pycache__'),
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=workspace,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    service.bind_resource_manager(manager)
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    service.resource_registry.refresh_from_current_resources()
    service.policy_engine.sync_default_role_policies()
    return service, manager


def _task_stub() -> SimpleNamespace:
    return SimpleNamespace(task_id='task:seed', session_id='web:shared')


async def _shutdown(service: MainRuntimeService, manager: ResourceManager) -> None:
    manager.close()
    await service.close()


@pytest.mark.asyncio
async def test_resolver_accepts_visible_tools_in_declaration_order(tmp_path: Path) -> None:
    service, manager = _build_service(tmp_path)
    try:
        resolved = service.resolve_node_seed_contracts(task=_task_stub(), node_kind='execution', declared=['seed_b', 'seed_a'])
        assert resolved['seed_tools'] == ['seed_b', 'seed_a']
        assert [item['tool'] for item in resolved['accepted']] == ['seed_b', 'seed_a']
        assert resolved['dropped'] == []
        assert all(int(item['chars']) > 0 for item in resolved['accepted'])
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_resolver_drops_invisible_names_for_the_target_role(tmp_path: Path) -> None:
    service, manager = _build_service(tmp_path)
    try:
        # execution 节点声明一个只给 ceo 的工具：判据按目标节点角色实时算，与名单来源无关。
        resolved = service.resolve_node_seed_contracts(task=_task_stub(), node_kind='execution', declared=['seed_ceo_only'])
        assert resolved['seed_tools'] == []
        assert resolved['dropped'] == [{'tool': 'seed_ceo_only', 'reason': 'not_visible_for_role'}]
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('declared', 'expected_reason'),
    [
        ('seed_repair', 'repair_required'),
        ('seed_no_skill', 'no_toolskill'),
        ('exec', 'already_always_callable'),
        ('seed_typo', 'unknown_tool'),
    ],
)
async def test_resolver_reports_each_rejection_with_its_own_reason(
    tmp_path: Path,
    declared: str,
    expected_reason: str,
) -> None:
    service, manager = _build_service(tmp_path)
    try:
        resolved = service.resolve_node_seed_contracts(
            task=_task_stub(), node_kind='execution', declared=[declared, 'seed_a']
        )
        assert resolved['seed_tools'] == ['seed_a'], '被拒的名字不能连累同批里合法的那个'
        assert resolved['dropped'] == [{'tool': declared, 'reason': expected_reason}]
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_repair_required_is_not_reported_as_a_role_problem(tmp_path: Path) -> None:
    """待修复资源在治理读口里同样不可见；必须先报 `repair_required`。

    反过来会把「可以提示修复」说成「这个角色用不了，别再报这个名字」，派发方从此不再报它。
    """
    service, manager = _build_service(tmp_path)
    try:
        visible = service.list_effective_tool_names(actor_role='execution', session_id='web:shared')
        assert 'seed_repair' not in visible, '前提：它在 RBAC 读口里确实不可见'
        resolved = service.resolve_node_seed_contracts(task=_task_stub(), node_kind='execution', declared=['seed_repair'])
        assert resolved['dropped'] == [{'tool': 'seed_repair', 'reason': 'repair_required'}]
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_resolver_never_prints_a_half_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """印不出块时跳过并记 `render_empty`，不许发一份没有身份的块。"""
    service, manager = _build_service(tmp_path)
    try:
        monkeypatch.setattr('main.service.runtime_service.build_tool_context_fingerprint', lambda payload: '')
        resolved = service.resolve_node_seed_contracts(task=_task_stub(), node_kind='execution', declared=['seed_a'])
        assert resolved['messages'] == []
        assert resolved['dropped'] == [{'tool': 'seed_a', 'reason': 'render_empty'}]
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_resolver_enforces_the_single_char_budget(tmp_path: Path) -> None:
    service, manager = _build_service(tmp_path)
    try:
        resolved = service.resolve_node_seed_contracts(task=_task_stub(), node_kind='execution', declared=['seed_fat', 'seed_a'])
        assert resolved['seed_tools'] == ['seed_a']
        assert resolved['dropped'] == [{'tool': 'seed_fat', 'reason': 'single_char_limit'}]
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_resolver_enforces_the_total_budget_and_keeps_later_lean_items(tmp_path: Path) -> None:
    service, manager = _build_service(tmp_path)
    try:
        resolved = service.resolve_node_seed_contracts(
            task=_task_stub(), node_kind='execution', declared=['seed_wide_1', 'seed_wide_2', 'seed_a']
        )
        assert resolved['seed_tools'] == ['seed_wide_1', 'seed_a']
        assert resolved['dropped'] == [{'tool': 'seed_wide_2', 'reason': 'total_char_limit'}]
        assert sum(int(item['chars']) for item in resolved['accepted']) <= NODE_SEED_CONTRACT_TOTAL_CHAR_LIMIT
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_every_seed_message_carries_exactly_one_recognised_block(tmp_path: Path) -> None:
    """一条消息恰好一块，且块头在第一个字符处——这是本增量的核心断言。

    `_leading_block_pairs` 锚在 `lstrip()` 之后，多块同一条只有第一个被认，其余静默失效。
    """
    service, manager = _build_service(tmp_path)
    try:
        resolved = service.resolve_node_seed_contracts(
            task=_task_stub(), node_kind='execution', declared=['seed_a', 'seed_b']
        )
        messages = resolved['messages']
        assert len(messages) == 2
        for message, expected_tool in zip(messages, ['seed_a', 'seed_b']):
            assert message['role'] == 'user'
            content = str(message['content'])
            assert content.lstrip().startswith(MARKER_PREFIX), '垫任何前导文字都会让块头认不出'
            assert content.count(MARKER_PREFIX) == 1, '一条消息只准一个块头'
            pairs = contract_pairs_from_value(content)
            assert len(pairs) == 1
            assert pairs[0][0] == expected_tool
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_seed_block_bytes_equal_the_loader_delivery(tmp_path: Path) -> None:
    """预置与 loader 交付必须逐字节同一份块，否则在场判据认不出预置的那一份。"""
    service, manager = _build_service(tmp_path)
    try:
        loaded = service.load_tool_context(actor_role='execution', session_id='web:shared', tool_id='seed_a')
        loader_block = render_tool_contract(loaded)
        resolved = service.resolve_node_seed_contracts(task=_task_stub(), node_kind='execution', declared=['seed_a'])
        assert loader_block
        assert resolved['messages'][0]['content'] == loader_block
        assert resolved['accepted'][0]['rev'] == str(loaded.get('tool_context_fingerprint') or '')
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_resolver_returns_everything_empty_without_declaration(tmp_path: Path) -> None:
    service, manager = _build_service(tmp_path)
    try:
        resolved = service.resolve_node_seed_contracts(task=_task_stub(), node_kind='execution', declared=[])
        assert resolved == {'seed_tools': [], 'messages': [], 'accepted': [], 'dropped': []}
    finally:
        await _shutdown(service, manager)


# --- bootstrap 落位 -----------------------------------------------------------


async def _make_task(service: MainRuntimeService):
    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )
    record = await service.create_task(
        'seed lane task',
        session_id='web:shared',
        metadata={'core_requirement': 'seed lane'},
    )
    return record, service.get_node(record.root_node_id)


async def _make_seeded_node(service: MainRuntimeService, *, seed_tools: list[str] | None):
    """车道进口：预置名单只能由派发子节点的父节点声明（前门那半已撤）。"""
    record, root = await _make_task(service)
    child = service.node_runner._create_execution_child(
        task=service.get_task(record.task_id),
        parent=service.get_node(root.node_id),
        spec=SpawnChildSpec(
            goal='seeded child',
            prompt='seeded child prompt',
            execution_policy={'mode': 'focus'},
            seed_tools=list(seed_tools or []),
        ),
    )
    return record, child


@pytest.mark.asyncio
async def test_bootstrap_inserts_seed_messages_after_payload(tmp_path: Path) -> None:
    service, manager = _build_service(tmp_path)
    try:
        record, node = await _make_seeded_node(service, seed_tools=['seed_a', 'seed_b'])
        messages = await service.node_runner._build_messages(task=service.get_task(record.task_id), node=node)
        assert messages[0]['role'] == 'system'
        assert str(messages[1]['content']).lstrip().startswith('{'), 'bootstrap payload 仍是第二条'
        assert str(messages[2]['content']).lstrip().startswith(MARKER_PREFIX)
        assert str(messages[3]['content']).lstrip().startswith(MARKER_PREFIX)
        # 预置插在稳定前缀里，不越到末尾去，也不替节点重新引一份活状态尾块（尾块已退役）。
        assert len(messages) == 4
        assert not any(
            str(item.get('content') or '').startswith(NODE_DYNAMIC_CONTRACT_HEADING) for item in messages
        ), '预置不该把退役的尾块带回来'
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_bootstrap_is_byte_identical_across_rebuilds(tmp_path: Path) -> None:
    """钉住一次定稿：同一节点两次构造逐字节相同（toolskill 源在这两次之间没动）。"""
    service, manager = _build_service(tmp_path)
    try:
        record, root = await _make_seeded_node(service, seed_tools=['seed_a'])
        task = service.get_task(record.task_id)
        node = service.get_node(root.node_id)
        first = await service.node_runner._build_messages(task=task, node=node)
        second = await service.node_runner._build_messages(task=task, node=node)
        assert [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in first] == [
            json.dumps(item, ensure_ascii=False, sort_keys=True) for item in second
        ]
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_bootstrap_message_count_unchanged_without_declaration(tmp_path: Path) -> None:
    """没声明 ⇒ 一条消息都不多插，节点行为与改造前一致（回归钉住）。"""
    service, manager = _build_service(tmp_path)
    try:
        record, node = await _make_seeded_node(service, seed_tools=None)
        messages = await service.node_runner._build_messages(task=service.get_task(record.task_id), node=node)
        assert not any(str(item.get('content') or '').lstrip().startswith(MARKER_PREFIX) for item in messages)
        assert len(messages) == 2, 'system + payload'
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_seed_observations_land_in_node_metadata_and_frame(tmp_path: Path) -> None:
    service, manager = _build_service(tmp_path)
    try:
        record, root = await _make_seeded_node(service, seed_tools=['seed_a', 'seed_ceo_only'])
        task = service.get_task(record.task_id)
        await service.node_runner._build_messages(task=task, node=service.get_node(root.node_id))
        metadata = dict(service.get_node(root.node_id).metadata or {})
        assert [item['tool'] for item in metadata['seeded_contracts']] == ['seed_a']
        assert metadata['seeded_dropped'] == [{'tool': 'seed_ceo_only', 'reason': 'not_visible_for_role'}]
        frame = service.log_service.read_runtime_frame(record.task_id, root.node_id) or {}
        assert frame['seeded_contracts'][0]['tool'] == 'seed_a'
        assert frame['seeded_dropped'][0]['reason'] == 'not_visible_for_role'
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_first_hop_executes_a_seeded_tool_without_the_impulse_lane(tmp_path: Path) -> None:
    """本增量的收益断言：预置块活到当次请求视图后，首跳就**真的执行**，不再只交契约。

    入口用 `ReActToolLoop._execute_tool_raw`——契约在场准入、冲动投递与 `execution_path`
    记账都在这一个口上同时成立；直接调渲染器证不了准入。
    """
    from g3ku.runtime.tool_context_presence import contract_presence_index
    from main.runtime.react_loop import ReActToolLoop

    service, manager = _build_service(tmp_path)
    try:
        record, root = await _make_seeded_node(service, seed_tools=['seed_a'])
        task = service.get_task(record.task_id)
        node = service.get_node(root.node_id)
        messages = await service.node_runner._build_messages(task=task, node=node)
        presence = contract_presence_index(request_messages=messages)
        assert 'seed_a' in presence and 'seed_b' not in presence

        loop = ReActToolLoop(chat_backend=SimpleNamespace(), log_service=SimpleNamespace(), max_iterations=2)
        loop._tool_impulse_service = service
        runtime_context: dict[str, Any] = {
            'task_id': record.task_id,
            'node_id': root.node_id,
            'session_key': 'web:shared',
            'actor_role': 'execution',
            'tool_contract_presence': presence,
            'tool_execution_paths': {},
            'current_tool_call_id': 'call:seed',
        }

        seeded = await loop._execute_tool_raw(
            tools={'seed_a': service._resource_manager.get_tool('seed_a')},
            tool_name='seed_a',
            arguments={'value': 'hello'},
            runtime_context=runtime_context,
        )
        assert MARKER_PREFIX not in str(seeded), '预置生效时这一跳不该再是一份契约投递'
        assert json.loads(str(seeded))['ok'] is True
        # `normal` 是读数侧的隐式默认：只有 `reused` / `corrected` / `illegal_reuse` 才被记账，
        # 所以"这一跳是 normal"在这里的可证形式是**这条 call id 没有任何记账**。
        assert 'call:seed' not in runtime_context['tool_execution_paths']
        assert not runtime_context.get('impulse_staged_call_ids'), '预置工具不该被当成冲动调用暂存'

        # 回归保护：没预置的工具照旧走冲动路径，本增量没把那条道弄坏。
        unseeded_context = {**runtime_context, 'current_tool_call_id': 'call:unseeded'}
        unseeded = await loop._execute_tool_raw(
            tools={'seed_b': service._resource_manager.get_tool('seed_b')},
            tool_name='seed_b',
            arguments={'value': 'hello'},
            runtime_context=unseeded_context,
        )
        assert f'{MARKER_PREFIX}seed_b:' in str(unseeded), '未预置的名字仍应拿到契约而不是被执行'
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_create_async_task_result_has_no_seed_receipt(tmp_path: Path) -> None:
    """前门这一跳的结果文本只剩任务号：预置判据不由前门声明，也不由前门回执。

    走真 handler 逐字节读结果文本，并钉两件事：一条结果里只解析出一个任务号（"从结果文本
    抠本回合派了哪些任务"的解析器在整段文本上 findall，任何长得像 id 的追加值都会被当成又
    派了一个任务）；旧前门带着 `seed_tools` 重发时（长会话钉住的 `tools[]` 还认得这个字段）
    任务照常创建，根节点一个预置键都不落。
    """
    import re

    from g3ku.runtime.frontdoor._ceo_runtime_ops import _TASK_ID_PATTERN

    service, manager = _build_service(tmp_path)
    try:
        shutil.copytree(
            REPO_ROOT / 'tools' / 'create_async_task_cn',
            manager.workspace / 'tools' / 'create_async_task_cn',
            ignore=shutil.ignore_patterns('__pycache__'),
        )
        manager.reload_now(trigger='create-async-task-no-seed-test')
        tool = manager.get_tool('create_async_task')
        assert tool is not None
        # web 模式下 `create_task` 要求 worker 在线，这里是走真 handler 而不是直调 service。
        service.store.upsert_worker_status(
            worker_id='worker:test',
            role='task_worker',
            status='running',
            updated_at=now_iso(),
            payload={'execution_mode': 'worker', 'active_task_count': 0},
        )

        text = await tool.execute(
            task='seed lane dispatch',
            core_requirement='seed lane',
            execution_policy={'mode': 'focus'},
            seed_tools=['seed_a', 'seed_repair', 'seed_typo'],
        )
        assert str(text).startswith('创建任务成功')
        assert 'seed_tools' not in str(text), '前门结果文本不再带预置回执'
        ids = _TASK_ID_PATTERN.findall(str(text))
        assert len(ids) == 1, f'结果文本造出了额外任务号: {ids}'
        assert re.fullmatch(r'task:[0-9a-f]{12}', ids[0]), ids[0]

        root = service.get_node(service.get_task(ids[0]).root_node_id)
        metadata = dict(root.metadata or {})
        assert not ({'seed_tools', 'seed_receipt', 'seeded_contracts', 'seeded_dropped', 'seed_error'} & set(metadata)), (
            f'根节点落了预置键: {sorted(metadata)}'
        )
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_frame_readings_survive_every_frame_field_list(tmp_path: Path) -> None:
    """帧字段有四处各自重建字典的读点：写时 sanitize、落记录、hydrate、公开投影。

    漏一处不是报错而是**永远读不到**——面板与实时补丁拿的是公开投影那一份，
    所以逐层各读一次，而不是只证明其中一层能过。
    """
    service, manager = _build_service(tmp_path)
    try:
        record, root = await _make_seeded_node(service, seed_tools=['seed_a'])
        task = service.get_task(record.task_id)
        await service.node_runner._build_messages(task=task, node=service.get_node(root.node_id))

        stored = service.log_service.read_runtime_frame_payload(record.task_id, root.node_id) or {}
        assert [item['tool'] for item in stored.get('seeded_contracts') or []] == ['seed_a'], '写时 sanitize'

        hydrated = service.log_service.read_runtime_frame(record.task_id, root.node_id) or {}
        assert [item['tool'] for item in hydrated.get('seeded_contracts') or []] == ['seed_a'], 'hydrate 读点'

        public = service.log_service._public_runtime_frame(hydrated)
        assert [item['tool'] for item in public.get('seeded_contracts') or []] == ['seed_a'], '公开投影'
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_later_frame_write_without_the_readings_does_not_blanc_them(tmp_path: Path) -> None:
    """实盘抓到的形状：首跳之后 react_loop 每次 `upsert_frame` 都不带这两份读数。

    白名单把"没带"补成空列表，于是帧里预置成功过这件事被抹平——节点行一直是对的，只有实时
    那一份变空。写帧方没带 ⇒ 继承存量（与 `messages_ref` 同一条意规则）。
    """
    service, manager = _build_service(tmp_path)
    try:
        record, root = await _make_seeded_node(service, seed_tools=['seed_a'])
        task = service.get_task(record.task_id)
        await service.node_runner._build_messages(task=task, node=service.get_node(root.node_id))
        before = service.log_service.read_runtime_frame_payload(record.task_id, root.node_id) or {}
        assert [item['tool'] for item in before.get('seeded_contracts') or []] == ['seed_a']

        # 模拟 react_loop 的一次普通写帧：整份帧重建，完全不认识 seeded_* 这两个键。
        service.log_service.upsert_frame(
            record.task_id,
            {
                'node_id': root.node_id,
                'depth': 0,
                'node_kind': 'execution',
                'phase': 'before_model',
                'messages': [{'role': 'system', 'content': 'x'}],
                'callable_tool_names': ['seed_a'],
            },
            publish_snapshot=False,
        )
        after = service.log_service.read_runtime_frame_payload(record.task_id, root.node_id) or {}
        assert [item['tool'] for item in after.get('seeded_contracts') or []] == ['seed_a'], '后续写帧把读数抹平了'
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_first_hop_sees_the_seeded_contract_in_the_request_view(tmp_path: Path) -> None:
    """预置的落点必须是**当次请求视图**：在场判据只读那里，读别处都是白插。"""
    service, manager = _build_service(tmp_path)
    try:
        record, root = await _make_seeded_node(service, seed_tools=['seed_a'])
        task = service.get_task(record.task_id)
        node = service.get_node(root.node_id)
        messages = await service.node_runner._build_messages(task=task, node=node)
        index = service.contract_present_tool_names_for_node(
            actor_role='execution',
            session_id='web:shared',
            request_messages=messages,
        )
        assert 'seed_a' in index
        assert 'seed_a' in service._callable_tool_names_for_node(task=task, node=node, request_messages=messages)
        assert 'seed_b' not in service._callable_tool_names_for_node(task=task, node=node, request_messages=messages)
    finally:
        await _shutdown(service, manager)


@pytest.mark.asyncio
async def test_dispatched_receipt_is_persisted_on_the_child_node(tmp_path: Path) -> None:
    service, manager = _build_service(tmp_path)
    try:
        record, root = await _make_task(service)
        task = service.get_task(record.task_id)
        root_metadata = dict(service.get_node(root.node_id).metadata or {})
        assert not {'seed_tools', 'seed_receipt', 'seeded_contracts', 'seeded_dropped'} & set(root_metadata), (
            '前门派发的根节点不再带任何预置判据'
        )

        child = service.node_runner._create_execution_child(
            task=task,
            parent=service.get_node(root.node_id),
            spec=SpawnChildSpec(
                goal='g',
                prompt='p',
                execution_policy={'mode': 'focus'},
                seed_tools=['seed_b', 'seed_typo'],
            ),
        )
        child_metadata = dict(child.metadata or {})
        assert child_metadata['seed_tools'] == ['seed_b']
        assert child_metadata['seed_receipt']['dropped'] == [{'tool': 'seed_typo', 'reason': 'unknown_tool'}]
        assert service.node_runner._seed_receipt_for_node(child.node_id) == child_metadata['seed_receipt']
    finally:
        await _shutdown(service, manager)


def test_seed_tools_guidance_lives_where_the_provider_does_not_strip_it() -> None:
    """字段级 description 到不了模型：`sanitize_provider_parameters_schema` 递归删掉它。

    实盘一份前门请求体里 `create_async_task.file_targets` 只剩 `{"type":"array",...}`，
    一个字说明都没有。所以"派发方该怎么用这个字段"必须写在**工具级** description 上；
    这条用例钉住它在那一层，而不是被搬回字段里悄悄失效。
    """
    from g3ku.json_schema_utils import normalize_openai_tool_definition
    from main.runtime.internal_tools import SpawnChildNodesTool

    tool = SpawnChildNodesTool(lambda *_args, **_kwargs: [])
    function = normalize_openai_tool_definition(tool.to_model_schema())['function']
    assert 'seed_tools' in str(function['description'])
    serialized_parameters = json.dumps(function['parameters'], ensure_ascii=False)
    assert 'seed_tools' in serialized_parameters, '字段名要过闸'
    assert 'Optional. Tool names whose contracts' not in serialized_parameters, '字段说明不该以为能过闸'


def test_create_async_task_advertises_no_seed_tools() -> None:
    """前门模型面（`resource.yaml` 顶层 description + parameters）不认得预置字段。

    长会话的 `tools[]` 会钉住到压缩才刷新，所以撤字段要连工具级说明一起撤：留着名字，
    旧前门照旧会去声明，而声明出来的判据现在无处落。
    """
    import yaml

    manifest = yaml.safe_load(
        (REPO_ROOT / 'tools' / 'create_async_task_cn' / 'resource.yaml').read_text(encoding='utf-8-sig')
    )
    assert 'seed_tools' not in str(manifest.get('description') or '')
    assert 'seed_tools' not in json.dumps(manifest.get('parameters') or {}, ensure_ascii=False)
    assert 'seed_tools' not in str((manifest.get('governance') or {}).get('description') or '')

    from main.service.create_async_task_contract import build_create_async_task_parameters

    assert 'seed_tools' not in json.dumps(build_create_async_task_parameters(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_spawn_round_delivery_attaches_the_child_receipt(tmp_path: Path) -> None:
    """父节点收到的那一侧：回执必须穿过**交付漏斗**，不是只躺在子节点 metadata 里。

    `spawn_child_nodes` 的结果整轮由 `_spawn_round_results_from_entries` 重建后交付，
    前面两条用例分别证了"写进 metadata"和"读得出来"，中间这一层按字段重建结果对象，
    漏了它就只有子节点自己知道预置过什么，派发方永远等不到判据。
    """
    service, manager = _build_service(tmp_path)
    try:
        record, root = await _make_seeded_node(service, seed_tools=['seed_a'])
        task = service.get_task(record.task_id)
        parent = service.get_node(root.node_id)
        seeded = service.node_runner._create_execution_child(
            task=task,
            parent=parent,
            spec=SpawnChildSpec(goal='seeded child', prompt='p', execution_policy={'mode': 'focus'}, seed_tools=['seed_b', 'seed_typo']),
        )
        plain = service.node_runner._create_execution_child(
            task=task,
            parent=parent,
            spec=SpawnChildSpec(goal='plain child', prompt='q', execution_policy={'mode': 'focus'}),
        )
        entries = [
            {'goal': 'seeded child', 'child_node_id': seeded.node_id, 'requires_acceptance': False},
            {'goal': 'plain child', 'child_node_id': plain.node_id, 'requires_acceptance': False},
        ]

        results = service.node_runner._spawn_round_results_from_entries(
            task_id=record.task_id,
            entries=entries,
            specs=[
                SpawnChildSpec(goal='seeded child', prompt='p', execution_policy={'mode': 'focus'}, seed_tools=['seed_b', 'seed_typo']),
                SpawnChildSpec(goal='plain child', prompt='q', execution_policy={'mode': 'focus'}),
            ],
        )
        assert len(results) == 2
        by_goal = {item.goal: item for item in results}

        receipt = by_goal['seeded child'].seed_tools
        assert receipt is not None, '交付漏斗没把回执挂上，派发方看不见判据'
        assert [item['tool'] for item in receipt['accepted']] == ['seed_b']
        assert receipt['dropped'] == [{'tool': 'seed_typo', 'reason': 'unknown_tool'}]
        # 派发方真声明过就要有判据，哪怕全被丢：这里 accepted 非空、dropped 非空，两键都在。
        assert set(receipt) == {'accepted', 'dropped'}

        # 没声明的那条子分支：键整个不存在，而不是回一份空回执。
        dumped = {item.goal: item.model_dump(mode='json', exclude_none=True) for item in results}
        assert 'seed_tools' not in dumped['plain child']
    finally:
        await _shutdown(service, manager)


def test_spawn_child_spec_keeps_the_declared_names_through_validation() -> None:
    """pydantic 基类默认忽略多余键：只改工具 schema 不改模型，名单会被静默吞掉。"""
    spec = SpawnChildSpec.model_validate(
        {'goal': 'g', 'prompt': 'p', 'execution_policy': {'mode': 'focus'}, 'seed_tools': ['seed_b', 'seed_b', ' seed_a ']}
    )
    assert spec.seed_tools == ['seed_b', 'seed_b', ' seed_a ']


def test_spawn_child_result_omits_the_receipt_when_nothing_was_declared() -> None:
    """未声明时 `seed_tools` 键整个消失：空回执对派发方是噪音，而不是「全被丢弃」。"""
    assert 'seed_tools' not in SpawnChildResult(goal='g').model_dump(mode='json', exclude_none=True)
    with_receipt = SpawnChildResult(goal='g', seed_tools={'accepted': [], 'dropped': []})
    assert with_receipt.model_dump(mode='json', exclude_none=True)['seed_tools'] == {'accepted': [], 'dropped': []}


@pytest.mark.asyncio
async def test_seed_parse_failure_does_not_block_dispatch(tmp_path: Path) -> None:
    """预置是优化不是前置条件：解析炸了任务照常建，只留 `seed_error`，不编造 reason。"""
    service, manager = _build_service(tmp_path)
    original = service.resolve_node_seed_contracts

    def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError('registry unreadable')

    try:
        service.resolve_node_seed_contracts = _boom  # type: ignore[method-assign]
        record, root = await _make_seeded_node(service, seed_tools=['seed_a'])
        metadata = dict((service.get_node(root.node_id) or root).metadata or {})
        assert record.task_id
        assert metadata.get('seed_tools') == ['seed_a'], '名单照原样留下，首跳还会再解一次'
        assert 'registry unreadable' in str(metadata.get('seed_error') or '')
        assert 'seed_receipt' not in metadata, '读不出来就不给派发方一份编造的判据'
    finally:
        service.resolve_node_seed_contracts = original  # type: ignore[method-assign]
        await _shutdown(service, manager)
