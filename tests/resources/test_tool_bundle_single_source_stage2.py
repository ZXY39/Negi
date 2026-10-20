"""阶段 2 节点道：`tools[]` 参数表只从钉住整表取字节（开关 `G3KU_TOOLS_BUNDLE_SINGLE_SOURCE`）。

钉住的合同是"成员与字节都只在重印边界变"。改读整表之前，派发字典里的名字每跳由活实例现渲，
运维改一颗工具的参数定义就会在边界之外换字节；整表缺名的那一跳则会退回活实例或悄悄摘名。
这两条用例分别钉住"编辑不进冻结跳"和"缺名不摘名、就地重建"。
"""

from __future__ import annotations

from types import SimpleNamespace

from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService

_TASK_ID = 'task-bundle-stage2'
_NODE_ID = 'node-bundle-stage2'


class _Tool:
    def __init__(self, name: str, description: str = 'desc') -> None:
        self.name = name
        self.description = description
        self.parameters: dict = {}
        self._description = description

    def rewrite(self, description: str) -> None:
        self._description = description

    def to_model_schema(self) -> dict:
        return {
            'type': 'function',
            'function': {
                'name': self.name,
                'description': self._description,
                'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}},
            },
        }


class _ArtifactStore:
    def __init__(self) -> None:
        self.texts: dict[str, str] = {}
        self.writes = 0

    def create_or_replace_singleton_text_artifact(self, **kwargs) -> SimpleNamespace:
        # singleton 的语义要照真的来：同 (task,node,kind) 复用同一个 id，就地换内容。
        self.writes += 1
        artifact_id = 'artifact:pinned-bundle-1'
        self.texts[artifact_id] = str(kwargs.get('content') or '')
        return SimpleNamespace(artifact_id=artifact_id)


class _LogService:
    def __init__(self, frame: dict | None = None) -> None:
        self.frame = dict(frame or {})
        self.store = _ArtifactStore()
        self.resolves = 0

    def read_runtime_frame(self, task_id: str, node_id: str) -> dict | None:
        return dict(self.frame) if self.frame else None

    def resolve_content_ref(self, ref: str) -> str:
        self.resolves += 1
        return self.store.texts.get(str(ref or '').strip(), '')


def _service(*, frame: dict | None, tools: dict[str, _Tool]) -> MainRuntimeService:
    service = object.__new__(MainRuntimeService)
    service.log_service = _LogService(frame=frame)
    service.artifact_store = service.log_service.store
    service.store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(
            task_id=task_id, session_id='web:shared', metadata={'core_requirement': 'do the work'}
        ),
        get_node=lambda node_id: SimpleNamespace(
            node_id=node_id, prompt='do the work', goal='do the work', node_kind='execution'
        ),
    )
    service._resource_manager = SimpleNamespace(tool_instances=lambda: dict(tools))
    service.execution_visible_tool_lightweight_items = lambda *, actor_role, session_id: []
    service.list_effective_tool_names = lambda *, actor_role, session_id: list(tools.keys())
    return service


def _select(service, tools: dict[str, _Tool]) -> dict:
    return service._select_model_visible_tool_schema_payload(
        task_id=_TASK_ID,
        node_id=_NODE_ID,
        node_kind='execution',
        visible_tools=tools,
        runtime_context={'task_id': _TASK_ID, 'node_id': _NODE_ID, 'session_key': 'web:shared', 'actor_role': 'execution'},
    )


def _frame_with_bundle(ref: str, revision: str, names: list[str]) -> dict:
    return {
        'node_id': _NODE_ID,
        'model_visible_tool_names': list(names),
        'provider_tool_names': list(names),
        'model_visible_tool_selection_trace': {
            'declaration_schema_ref': ref,
            'bundle_ref': ref,
            'bundle_revision': revision,
        },
    }


def test_flag_off_keeps_the_old_chain_and_hands_over_no_index(monkeypatch) -> None:
    monkeypatch.delenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', raising=False)
    tools = {'alpha': _Tool('alpha'), 'beta': _Tool('beta')}
    service = _service(frame=None, tools=tools)

    selection = _select(service, tools)

    assert selection['pinned_tool_bundle_index'] == {}
    assert selection['trace']['provider_tool_bundle_mode'] == 'pinned_seeded'
    # 种子跳仍然写整表（这条一直如此），但读路径不消费它。
    assert selection['trace']['declaration_schema_ref']


def test_flag_on_seed_hop_builds_bundle_and_wire_bytes_match_it(monkeypatch) -> None:
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    tools = {'alpha': _Tool('alpha'), 'beta': _Tool('beta')}
    service = _service(frame=None, tools=tools)

    selection = _select(service, tools)
    index = dict(selection['pinned_tool_bundle_index'] or {})
    assert index, 'seed 跳之后必须交出整表索引'
    wire = ReActToolLoop._render_provider_tool_schemas(
        provider_tool_names=list(selection['provider_tool_names']),
        current_tools=tools,
        tool_schema_selection=selection,
    )
    assert [str((item.get('function') or {}).get('name')) for item in wire] == list(index.keys())
    assert wire == [index[name] for name in index]
    assert selection['trace']['bundle_revision']


def test_flag_on_schema_edit_does_not_change_a_frozen_hop(monkeypatch) -> None:
    """D1 / Q2-A：改一颗工具的参数定义，冻结跳的字节必须是上一份整表的字节。"""
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    tools = {'alpha': _Tool('alpha', 'v1'), 'beta': _Tool('beta')}
    service = _service(frame=None, tools=tools)
    seed = _select(service, tools)
    ref = str(seed['trace']['bundle_ref'] or seed['trace']['declaration_schema_ref'])
    revision = str(seed['trace']['bundle_revision'])

    tools['alpha'].rewrite('v2 edited outside any reprint boundary')
    service.log_service.frame = _frame_with_bundle(ref, revision, list(seed['provider_tool_names']))
    frozen = _select(service, tools)
    assert frozen['trace']['provider_tool_bundle_mode'] == 'pinned_frozen'
    wire = ReActToolLoop._render_provider_tool_schemas(
        provider_tool_names=list(frozen['provider_tool_names']),
        current_tools=tools,
        tool_schema_selection=frozen,
    )
    descriptions = {
        str((item.get('function') or {}).get('name')): str((item.get('function') or {}).get('description'))
        for item in wire
    }
    assert descriptions['alpha'] == 'v1'
    assert service.log_service.frame['provider_tool_names'] == frozen['provider_tool_names']


def test_flag_on_bundle_missing_name_rebuilds_in_place_without_dropping_it(monkeypatch) -> None:
    """整表缺名（存量第一份整表还没有后来的成员）⇒ 就地重建，名字不许从清单里掉出去。"""
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    tools = {'alpha': _Tool('alpha'), 'beta': _Tool('beta')}
    service = _service(frame=None, tools={'alpha': tools['alpha']})
    seed = _select(service, {'alpha': tools['alpha']})
    stale_ref = str(seed['trace']['declaration_schema_ref'])
    stale_revision = str(seed['trace']['bundle_revision'])

    # 钉住名单里现在有两个名字，而整表只有 alpha：beta 在④里没有条目。
    service.log_service.frame = _frame_with_bundle(stale_ref, stale_revision, ['alpha', 'beta'])
    selection = _select(service, tools)
    index = dict(selection['pinned_tool_bundle_index'] or {})
    assert set(index) >= {'alpha', 'beta'}
    assert selection['trace']['bundle_revision'] != stale_revision, '重建必须重写整表并换 revision'
    assert selection['trace']['bundle_fault'] == 0
    # 兼容重建（整表已经有、只是缺后来新增的成员）单独可数，别和真故障混在一栏。
    assert selection['trace']['bundle_compat_rebuild'] == 1
    wire = ReActToolLoop._render_provider_tool_schemas(
        provider_tool_names=list(selection['provider_tool_names']),
        current_tools=tools,
        tool_schema_selection=selection,
    )
    assert [str((item.get('function') or {}).get('name')) for item in wire][:2] == ['alpha', 'beta']


def test_flag_on_unrenderable_name_is_pinned_as_placeholder_in_bundle(monkeypatch) -> None:
    """三份来源都取不到 ⇒ 占位跟着进整表：不写进整表，下一跳又会缺名重建一次并再记一次 fault。"""
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    alive = {'alpha': _Tool('alpha')}
    service = _service(frame=None, tools=alive)
    seed = _select(service, alive)
    ref = str(seed['trace']['declaration_schema_ref'])
    revision = str(seed['trace']['bundle_revision'])

    _ = _Tool("ghost")  # 只造"名单里有、盘上没有"的名字，实例本身不进任何字典
    service.log_service.frame = _frame_with_bundle(ref, revision, ['alpha', 'ghost'])
    selection = _select(service, alive)  # ghost 既无实例也无快照，也不在旧整表里
    index = dict(selection['pinned_tool_bundle_index'] or {})
    assert 'ghost' in index
    description = str((index['ghost'].get('function') or {}).get('description') or '')
    assert 'not carried into this turn' in description
    # 占位的可数落点是整表这一栏，不是回填链那一栏：单源下五档链根本没跑，
    # 拿 `declaration_schema_placeholders` 当"有没有占位"的读数会在开关打开后永远读到空。
    assert selection['trace']['bundle_placeholder_names'] == ['ghost']
    assert selection['trace']['declaration_schema_placeholders'] == []
    # 尾块 `denied_tools` 现在按名单判（"在 tools[] 里"对整表供出的名字恒真），占位名因此
    # 被点名——这是尾块正文的一次真实变化，必须有用例钉住。
    assert 'ghost' in selection['declared_denied_tool_names']


def test_bundle_index_cache_is_keyed_by_ref_and_revision(monkeypatch) -> None:
    """F2：singleton 就地覆盖复用同一个 ref ⇒ 只按 ref 缓存会供旧字节。"""
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    tools = {'alpha': _Tool('alpha')}
    service = _service(frame=None, tools=tools)
    seed = _select(service, tools)
    ref = str(seed['trace']['declaration_schema_ref'])
    revision = str(seed['trace']['bundle_revision'])

    assert service._pinned_tool_schema_bundle_index(bundle_ref=ref, bundle_revision=revision)
    resolves_after_first_read = service.log_service.resolves
    assert service._pinned_tool_schema_bundle_index(bundle_ref=ref, bundle_revision=revision)
    assert service.log_service.resolves == resolves_after_first_read, '同一 (ref, revision) 不该再读盘'
    # 旧 revision 不落缓存：宁可每次读盘，也不供可能过期的内容。
    assert service._pinned_tool_schema_bundle_index(bundle_ref=ref, bundle_revision='')
    assert service.log_service.resolves > resolves_after_first_read


def test_zero_byte_bundle_reads_as_missing(tmp_path, monkeypatch) -> None:
    """G5：0 字节 / 解析失败判为"读不到"，不能当成"名单为空"后悄悄摘名。"""
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    service = _service(frame=None, tools={'alpha': _Tool('alpha')})
    empty = tmp_path / 'empty.json'
    empty.write_text('', encoding='utf-8')
    service.log_service.store.texts['artifact:empty'] = ''
    assert service._pinned_tool_schema_bundle_index(bundle_ref='artifact:empty', bundle_revision='r1') == {}
    service.log_service.store.texts['artifact:broken'] = '{not json'
    assert service._pinned_tool_schema_bundle_index(bundle_ref='artifact:broken', bundle_revision='r1') == {}
    assert service._pinned_tool_schema_bundle_index(bundle_ref='', bundle_revision='r1') == {}


def test_unreadable_bundle_reprints_once_and_keeps_every_name(monkeypatch) -> None:
    """读不出整表的那一跳：按当轮可见源重建，一个名字都不许从清单里掉出去。

    这一条把"0 字节被读成空名单 ⇒ 摘名"的分岔钉住：判为读不到之后走的是重建，重建出来的名单
    仍是完整名单，只是这一跳重铺一次尾部。
    """
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    tools = {'alpha': _Tool('alpha'), 'beta': _Tool('beta')}
    service = _service(frame=None, tools=tools)
    seed = _select(service, tools)
    service.log_service.frame = _frame_with_bundle(
        str(seed['trace']['bundle_ref']), 'revision-that-no-longer-resolves', list(seed['provider_tool_names'])
    )
    # 载体还在（有 ref），内容读不出来 ⇒ 上一跳的名字一个都不能少。
    service.log_service.store.texts[str(seed['trace']['bundle_ref'])] = ''
    rebuilt = _select(service, tools)
    wire = ReActToolLoop._render_provider_tool_schemas(
        provider_tool_names=list(rebuilt['provider_tool_names']),
        current_tools=tools,
        tool_schema_selection=rebuilt,
    )
    assert [str((item.get('function') or {}).get('name')) for item in wire] == list(rebuilt['provider_tool_names'])
    assert rebuilt['trace']['bundle_compat_rebuild'] == 1


def test_bundle_write_failure_serves_this_hop_from_memory_and_counts_a_fault(monkeypatch) -> None:
    """写盘失败不得退回去读活实例：这一跳照旧按重建出的整表字节发，并记 `bundle_fault`。

    回退读活实例会让"整表说的是什么"与"线上发的是什么"再次分家——那正是本次改造要买的性质。
    """
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    tools = {'alpha': _Tool('alpha', 'v1'), 'beta': _Tool('beta')}
    service = _service(frame=None, tools=tools)
    seed = _select(service, tools)
    ref = str(seed['trace']['bundle_ref'])
    revision = str(seed['trace']['bundle_revision'])

    tools['alpha'].rewrite('v2 edited outside any reprint boundary')
    service.log_service.frame = _frame_with_bundle(ref, revision, ['alpha', 'beta', 'ghost'])
    # 让 singleton 写入抛错：整表重写不出去。
    service.artifact_store = SimpleNamespace(
        create_or_replace_singleton_text_artifact=lambda **kwargs: (_ for _ in ()).throw(RuntimeError('disk full'))
    )
    selection = _select(service, tools)

    assert selection['trace']['bundle_fault'] == 1
    # ref 仍指上一份载体（就地覆盖的 singleton 没有被这次写换掉），revision 也一样：
    # 读数要说"这次没写进去"，不能伪报成"写成了新的一份"。
    assert selection['pinned_tool_bundle_ref'] == ref
    assert selection['trace']['bundle_revision'] == revision
    index = dict(selection['pinned_tool_bundle_index'] or {})
    assert set(index) >= {'alpha', 'beta', 'ghost'}
    # alpha 仍在整表里 ⇒ 供的是旧字节（没退回活实例）；ghost 三份来源都没有 ⇒ 定影成占位。
    assert str((index['alpha'].get('function') or {}).get('description')) == 'v1'
    assert 'ghost' in selection['trace']['bundle_placeholder_names']


def test_compat_rebuild_does_not_disturb_names_the_bundle_already_covers(monkeypatch) -> None:
    """冻结跳上补一个缺名，不能顺带把别人的字节换成当轮实例的新字节。

    兼容重建与边界重印的来源次序必须不同：前者只为缺的那个名字读实例，其余原样复用整表。
    """
    monkeypatch.setenv('G3KU_TOOLS_BUNDLE_SINGLE_SOURCE', '1')
    tools = {'alpha': _Tool('alpha', 'v1'), 'beta': _Tool('beta', 'b1')}
    service = _service(frame=None, tools={'alpha': tools['alpha']})
    seed = _select(service, {'alpha': tools['alpha']})

    tools['alpha'].rewrite('v2 edited outside any reprint boundary')
    service.log_service.frame = _frame_with_bundle(
        str(seed['trace']['bundle_ref']), str(seed['trace']['bundle_revision']), ['alpha', 'beta']
    )
    selection = _select(service, tools)
    index = dict(selection['pinned_tool_bundle_index'] or {})
    assert str((index['alpha'].get('function') or {}).get('description')) == 'v1', '兼容重建不许换别人的字节'
    assert str((index['beta'].get('function') or {}).get('description')) == 'b1', '缺的那个名字要补进来'
    assert selection['trace']['bundle_compat_rebuild'] == 1


def test_boundary_reprint_absorbs_a_schema_edit_for_dispatched_names() -> None:
    """重印跳是"按当轮实例重建整表"的合法时刻：编辑在这一跳被吸收，之后跟着新整表重新冻结。

    被收回、没进派发字典的名字仍取整表那份——否则一次无关的增删会顺带把别人的字节换掉。
    """
    tools = {'alpha': _Tool('alpha', 'v2 just edited'), 'beta': _Tool('beta', 'b1')}
    bundle_index = {
        'alpha': _Tool('alpha', 'v1').to_model_schema(),
        'beta': _Tool('beta', 'b1').to_model_schema(),
        'revoked': _Tool('revoked', 'old bytes').to_model_schema(),
    }
    selection = {'pinned_tool_bundle_index': bundle_index}

    frozen = ReActToolLoop._render_provider_tool_schemas(
        provider_tool_names=['alpha', 'beta', 'revoked'],
        current_tools={'alpha': tools['alpha'], 'beta': tools['beta']},
        tool_schema_selection=selection,
    )
    reprint = ReActToolLoop._render_provider_tool_schemas(
        provider_tool_names=['alpha', 'beta', 'revoked'],
        current_tools={'alpha': tools['alpha'], 'beta': tools['beta']},
        tool_schema_selection=selection,
        reprint=True,
    )
    def described(items: list[dict]) -> dict[str, str]:
        return {
            str((item.get('function') or {}).get('name')): str((item.get('function') or {}).get('description'))
            for item in items
        }

    assert described(frozen)['alpha'] == 'v1'
    assert described(reprint)['alpha'] == 'v2 just edited'
    assert described(reprint)['revoked'] == 'old bytes'
    assert [str((item.get('function') or {}).get('name')) for item in reprint] == ['alpha', 'beta', 'revoked']


def test_missing_bundle_name_renders_placeholder_not_live_bytes() -> None:
    """整表供不出的名字落占位：既不静默摘名，也不回退去读活实例。"""
    tools = {'alpha': _Tool('alpha', 'v2 from the live instance')}
    selection = {'pinned_tool_bundle_index': {'beta': _Tool('beta', 'b1').to_model_schema()}}

    rendered = ReActToolLoop._render_provider_tool_schemas(
        provider_tool_names=['alpha', 'beta'],
        current_tools=tools,
        tool_schema_selection=selection,
    )

    assert [str((item.get('function') or {}).get('name')) for item in rendered] == ['alpha', 'beta']
    alpha_description = str((rendered[0].get('function') or {}).get('description'))
    assert 'not carried into this turn' in alpha_description
