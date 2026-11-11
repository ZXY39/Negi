"""节点详情 full 档：轨迹按整轮窗口下发、尾部几轮全文直出、更早轮次与消息各走窄口。

实盘 `task:1d9cddf9858e`：详情响应 1.82 MB 里轨迹占 1.42 MB，且 full 档对**全部轮次**
的每个工具步做一次 artifact 读（O(轮数×工具数)）。这里钉住读侧的三件事：窗口边界永远
落在轮与轮之间、只有尾部几轮全文直出、更早轮次与 settle 消息各有自己的窄口。
"""

import copy
from types import SimpleNamespace

import main.monitoring.query_service as query_service_module
from main.monitoring.query_service import (
    _NODE_DETAIL_TRACE_PAGE_MAX_TURNS,
    NODE_DETAIL_TRACE_HYDRATED_TAIL_TURNS,
    NODE_DETAIL_TRACE_TURN_WINDOW,
    TaskQueryService,
)

TASK_ID = 'task:trace-window'
NODE_ID = 'node:root'


def _tool(seq: int, index: int) -> dict:
    return {
        'tool_call_id': f'call-{seq}-{index}',
        'tool_name': 'exec',
        'status': 'success',
        'arguments_text': '{"command":"ls"}',
        'output_text': f'preview-{seq}-{index}',
        'output_ref': f'artifact:out-{seq}-{index}',
    }


def _turn(seq: int) -> dict:
    return {
        'seq': seq,
        'text': f'进展 {seq}',
        'created_at': f'2026-11-11T10:00:{seq:02d}',
        'status': 'completed',
        'tools': [_tool(seq, 0), _tool(seq, 1)],
    }


def _trace(turn_count: int) -> dict:
    turns = [_turn(seq) for seq in range(1, turn_count + 1)]
    return {
        'initial_prompt': 'prompt',
        'final_output': '',
        'acceptance_result': '',
        'turns': turns,
        'tool_steps': [copy.deepcopy(tool) for turn in turns for tool in turn['tools']],
    }


class _DetailRecord:
    node_id = NODE_ID
    task_id = TASK_ID
    execution_trace_ref = 'artifact:trace-ref'

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __getattr__(self, name: str) -> str:
        return ''


class _Store:
    def __init__(
        self,
        payload: dict,
        *,
        node_task_id: str = TASK_ID,
        detail_task_id: str = TASK_ID,
    ) -> None:
        self._payload = payload
        self._node_task_id = node_task_id
        self.record = _DetailRecord(payload)
        self.record.task_id = detail_task_id
        self.record.updated_at = '2026-11-11T01:00:00+08:00'

    def get_task(self, task_id):
        return SimpleNamespace(task_id=task_id, root_node_id=NODE_ID)

    def get_task_node_detail(self, node_id):
        return self.record

    def get_task_node(self, node_id):
        return SimpleNamespace(node_id=node_id, task_id=self._node_task_id, payload={})

    def get_node(self, node_id):
        return None

    def get_task_node_pause(self, node_id):
        return None


class _LogService:
    """artifact 边界上的哨兵：解析过哪些 ref 一律记账，全文由 ref 派生便于逐条断言。"""

    def __init__(self, *, full_text_chars: int = 0) -> None:
        self.resolved_refs: list[str] = []
        self._full_text_chars = full_text_chars

    def resolve_node_input_text(self, runtime_node):  # pragma: no cover - runtime_node 恒为 None
        return ''

    def resolve_content_ref(self, ref):
        normalized = str(ref)
        self.resolved_refs.append(normalized)
        if self._full_text_chars:
            return 'x' * self._full_text_chars
        return f'full:{normalized}'


def _service(
    monkeypatch,
    *,
    turn_count: int = 5,
    window: int | None = 3,
    tail: int | None = 2,
    full_text_chars: int = 0,
    node_task_id: str = TASK_ID,
    detail_task_id: str = TASK_ID,
) -> tuple[TaskQueryService, _LogService, list[str]]:
    payload = {
        'node_id': NODE_ID,
        'task_id': TASK_ID,
        'execution_trace_summary': {},
        'execution_trace_ref': 'artifact:trace-ref',
        'token_usage_by_model': [],
    }
    log_service = _LogService(full_text_chars=full_text_chars)
    service = TaskQueryService(
        store=_Store(payload, node_task_id=node_task_id, detail_task_id=detail_task_id),
        file_store=None,
        log_service=log_service,
    )
    calls: list[str] = []
    trace = _trace(turn_count)

    def _spy_resolve(**kwargs):
        calls.append('resolve')
        return copy.deepcopy(trace)

    monkeypatch.setattr(service, '_resolve_execution_trace', _spy_resolve)
    monkeypatch.setattr(service, '_node_message_list', lambda **kwargs: [{'message': 'in'}])
    monkeypatch.setattr(service, '_node_sent_message_list', lambda **kwargs: [{'message': 'out'}])
    if window is not None:
        monkeypatch.setattr(query_service_module, 'NODE_DETAIL_TRACE_TURN_WINDOW', window)
    if tail is not None:
        monkeypatch.setattr(query_service_module, 'NODE_DETAIL_TRACE_HYDRATED_TAIL_TURNS', tail)
    return service, log_service, calls


def _seqs(trace: dict) -> list[int]:
    return [int(turn.get('seq') or 0) for turn in trace.get('turns') or []]


def _tool_output(turn: dict, index: int = 0) -> str:
    return str(turn['tools'][index].get('output_text') or '')


def test_window_boundary_falls_between_whole_turns(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, turn_count=5, window=3)
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level='full')

    assert _seqs(detail.execution_trace) == [3, 4, 5], '窗口只保留最近 3 轮，且按 seq 老→新'
    for turn in detail.execution_trace['turns']:
        assert len(turn['tools']) == 2, '边界落在轮与轮之间：被保留的轮不许只剩半个回合的工具'
        assert str(turn.get('text') or '').startswith('进展'), '轮的中途文本输出必须跟着这一轮一起下发'
    assert detail.execution_trace_window == {
        'total_turns': 5,
        'window_turns': 3,
        'truncated': True,
        'first_seq': 3,
        'last_seq': 5,
    }
    assert detail.execution_trace_total_turns == 5, '总轮数是全量数，前端「轮数 N」读它而不是数窗口'


def test_flat_tool_steps_converge_to_the_window(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, turn_count=5, window=3)
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level='full')

    assert [step['tool_call_id'] for step in detail.execution_trace['tool_steps']] == [
        'call-3-0', 'call-3-1', 'call-4-0', 'call-4-1', 'call-5-0', 'call-5-1',
    ], '扁平表收敛到窗口内轮次引用的 call id，不再随全量轮数增长'


def test_only_the_tail_turns_are_hydrated(monkeypatch) -> None:
    service, log, _calls = _service(monkeypatch, turn_count=5, window=3, tail=2)
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level='full')
    turns = {int(turn['seq']): turn for turn in detail.execution_trace['turns']}

    assert _tool_output(turns[4]) == 'full:artifact:out-4-0'
    assert _tool_output(turns[5], 1) == 'full:artifact:out-5-1'
    assert _tool_output(turns[3]) == 'preview-3-0', '窗口内非尾部轮保持预览，全文由前端按 ref 回取'
    assert turns[3]['tools'][0]['output_ref'] == 'artifact:out-3-0', 'ref 始终保留'
    assert [ref for ref in log.resolved_refs if ref.startswith('artifact:out-')] == [
        'artifact:out-4-0', 'artifact:out-4-1', 'artifact:out-5-0', 'artifact:out-5-1',
    ], 'artifact 读只花在尾部两轮上，不再是 O(全部轮次×工具数)'


def test_flat_tool_steps_are_never_hydrated(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, turn_count=5, window=3, tail=2)
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level='full')

    assert all(
        str(step.get('output_text') or '').startswith('preview-')
        for step in detail.execution_trace['tool_steps']
    ), '扁平表不再水合：前端在 turns 在场时不读它，summary 的 latest 5 步自己解析 ref'


def test_tail_full_output_is_capped_and_keeps_the_ref(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, turn_count=2, window=3, tail=2, full_text_chars=9000)
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level='full')
    step = detail.execution_trace['turns'][-1]['tools'][0]

    assert len(step['output_text']) <= 8000
    assert step['output_text'].endswith('...')
    assert step['output_truncated'] is True
    assert step['output_ref'] == 'artifact:out-2-0', '截断后 ref 仍在，前端「继续加载」按它分页'


def test_window_not_exceeded_keeps_every_turn(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, turn_count=2, window=3)
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level='full')

    assert _seqs(detail.execution_trace) == [1, 2]
    assert detail.execution_trace_window == {
        'total_turns': 2,
        'window_turns': 2,
        'truncated': False,
        'first_seq': 1,
        'last_seq': 2,
    }
    assert len(detail.execution_trace['tool_steps']) == 4


def test_shipped_default_window_slices_at_one_thousand_turns(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, turn_count=1001, window=None, tail=None)
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level='full')

    assert detail.execution_trace_total_turns == 1001
    assert detail.execution_trace_window['window_turns'] == NODE_DETAIL_TRACE_TURN_WINDOW == 1000
    assert detail.execution_trace_window['first_seq'] == 2
    assert detail.execution_trace_window['truncated'] is True
    assert len(detail.execution_trace['turns'][-1]['tools']) == 2


def test_summary_lane_is_untouched(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, turn_count=5, window=3)
    detail = service.get_node_detail(TASK_ID, NODE_ID, detail_level='summary')

    assert detail.execution_trace_window == {}, 'summary 档不切片，也不带窗口读数'
    assert detail.execution_trace_total_turns == 0
    latest = detail.execution_trace_summary.get('latest_tool_calls_full') or []
    assert len(latest) == 5, 'latest 5 步仍取自未切片轨迹'
    assert all(str(item.get('output_full') or '').startswith('full:') for item in latest)


def test_snapshot_lane_is_untouched(monkeypatch) -> None:
    service, _log, calls = _service(monkeypatch, turn_count=5, window=3)
    detail = service.get_node_detail(TASK_ID, NODE_ID, ship_execution_trace_body=False)

    assert calls == ['resolve'], '正文照旧解析——摘要依它而造'
    assert not detail.execution_trace
    assert detail.execution_trace_window == {}
    assert detail.execution_trace_total_turns == 0


def test_trace_turns_page_walks_backwards(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, turn_count=5, window=3)
    page = service.get_node_trace_turns(TASK_ID, NODE_ID, before_seq=4, limit=2)

    assert _seqs({'turns': page['turns']}) == [2, 3], '返回 before_seq 之前（不含）的最多 limit 轮，老→新'
    assert page['total_turns'] == 5
    assert page['returned_turns'] == 2
    assert page['has_more'] is True
    assert page['next_before_seq'] == 2, '下一拍用本页最老一轮的 seq 续翻'

    older = service.get_node_trace_turns(TASK_ID, NODE_ID, before_seq=page['next_before_seq'], limit=2)
    assert _seqs({'turns': older['turns']}) == [1]
    assert older['has_more'] is False, '更早再无轮次'
    assert older['next_before_seq'] == 1


def test_trace_turns_zero_anchor_returns_the_latest_window(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, turn_count=5, window=3)
    page = service.get_node_trace_turns(TASK_ID, NODE_ID, before_seq=0, limit=2)

    assert _seqs({'turns': page['turns']}) == [4, 5], 'before_seq<=0 退化为最新窗口，与 full 档切片同口径'
    assert page['has_more'] is True
    assert page['next_before_seq'] == 4


def test_trace_turns_limit_is_clamped(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, turn_count=5, window=3)

    # 路由层 Query(ge=1, le=1000) 已挡住 0 与超限；服务层再夹一次，负数落到 1。
    assert service.get_node_trace_turns(TASK_ID, NODE_ID, before_seq=0, limit=-5)['limit'] == 1
    assert service.get_node_trace_turns(TASK_ID, NODE_ID, before_seq=0, limit=99999)['limit'] == (
        _NODE_DETAIL_TRACE_PAGE_MAX_TURNS
    )


def test_trace_turns_page_keeps_preview_and_ref(monkeypatch) -> None:
    service, log, _calls = _service(monkeypatch, turn_count=5, window=3)
    page = service.get_node_trace_turns(TASK_ID, NODE_ID, before_seq=4, limit=2)
    step = page['turns'][0]['tools'][0]

    assert step['output_text'] == 'preview-2-0'
    assert step['output_ref'] == 'artifact:out-2-0'
    assert log.resolved_refs == [], '分页口不水合出参：与 full 档窗口内非尾部轮同形'


def test_trace_turns_rejects_a_foreign_node(monkeypatch) -> None:
    # 归属判据与 get_node_detail 同口径：明细行的 task_id 不是本任务就 404。
    service, _log, _calls = _service(monkeypatch, detail_task_id='task:other')

    assert service.get_node_trace_turns(TASK_ID, NODE_ID, before_seq=0, limit=2) is None


def test_messages_lane_only_ships_the_two_lists(monkeypatch) -> None:
    service, _log, calls = _service(monkeypatch, turn_count=5, window=3)
    payload = service.get_node_messages(TASK_ID, NODE_ID)

    assert set(payload) == {'node_id', 'task_id', 'message_list', 'sent_message_list'}
    assert payload['message_list'] == [{'message': 'in'}]
    assert payload['sent_message_list'] == [{'message': 'out'}]
    assert calls == [], 'settle 窄口不解析轨迹：每 2.5s 一拍的轮询不该付全轨迹解析'


def test_messages_lane_rejects_a_foreign_node(monkeypatch) -> None:
    service, _log, _calls = _service(monkeypatch, node_task_id='task:other')

    assert service.get_node_messages(TASK_ID, NODE_ID) is None


def test_shipped_defaults_are_the_documented_ones() -> None:
    # 哨兵：这三个数就是契约里写的那一档，被"顺手调大"时窗口/水合的成本会一起回来。
    assert NODE_DETAIL_TRACE_TURN_WINDOW == 1000
    assert NODE_DETAIL_TRACE_HYDRATED_TAIL_TURNS == 2
    assert _NODE_DETAIL_TRACE_PAGE_MAX_TURNS == 1000
