"""Where an error message tells its receiver to escalate, and who must not be told to fix config.

Nodes carry no ability to change model chains, provider keys or runtime configuration, so an
error that reaches a node has exactly one valid exit: pass it up. The line has to be
role-aware because only the root node's `send_message` accepts `frontdoor` — printing that
address for a deeper node points it at a `frontdoor_root_only` rejection. The front-door
lane needs no such pointer: its own failure already lands in the session as
「这一轮处理失败：…」 (transcript + web feed + channel notice).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from main.models import NodeRecord
from main.runtime.node_mailbox import error_escalation_hint
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService

TASK = 'task:escalate'
ROOT = 'node:root'
MID = 'node:mid'
LEAF = 'node:leaf'


def _service(tmp_path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )


def _seed_tree(service: MainRuntimeService) -> None:
    stamp = '2026-10-25T22:00:00+08:00'
    for node_id, parent, depth in ((ROOT, None, 0), (MID, ROOT, 1), (LEAF, MID, 2)):
        service.store.upsert_node(
            NodeRecord(
                node_id=node_id,
                task_id=TASK,
                parent_node_id=parent,
                root_node_id=ROOT,
                depth=depth,
                goal=f'goal-{node_id}',
                prompt='p',
                created_at=stamp,
                updated_at=stamp,
            )
        )


class TestHintBuilder:
    def test_root_gets_the_front_door_address(self):
        text = error_escalation_hint(receiver_is_root=True)
        assert 'to="frontdoor"' in text
        assert 'to="parent"' not in text

    def test_non_root_is_not_pointed_at_a_lane_that_rejects_it(self):
        text = error_escalation_hint(receiver_is_root=False)
        assert 'to="parent"' in text
        assert 'to="frontdoor"' not in text
        assert 'only the root node can reach the front door' in text

    def test_both_arms_declare_the_capability_boundary(self):
        for root_case in (True, False):
            text = error_escalation_hint(receiver_is_root=root_case)
            assert 'not yours to change' in text
            assert 'Model chains' in text


class TestAbnormalPauseMessageCarriesTheExit:
    async def _capture(self, service: MainRuntimeService) -> str:
        captured: dict[str, str] = {}

        async def _spy(**payload):
            captured['body'] = str(payload.get('message') or '')
            return {'status': 'delivered', 'target': payload.get('target_node_id'), 'kind': payload.get('system_kind')}

        service.deliver_node_message = _spy  # type: ignore[method-assign]
        await service._deliver_child_abnormal_pause(
            task_id=TASK, node_id=LEAF, reason='400 inference request is invalid'
        )
        return captured.get('body', '')

    async def test_delivered_to_a_mid_parent_reads_the_upward_exit(self, tmp_path):
        service = _service(tmp_path)
        _seed_tree(service)
        # LEAF 的父节点是 MID（非根）
        body = await self._capture(service)
        assert 'to="parent"' in body
        assert '400 inference request is invalid' in body
        assert 'to="frontdoor"' not in body

    async def test_root_parent_gets_the_front_door_exit(self, tmp_path):
        service = _service(tmp_path)
        _seed_tree(service)
        captured: dict[str, str] = {}

        async def _spy(**payload):
            captured['body'] = str(payload.get('message') or '')
            return {'status': 'delivered'}

        service.deliver_node_message = _spy  # type: ignore[method-assign]
        await service._deliver_child_abnormal_pause(task_id=TASK, node_id=MID, reason='boom')
        assert 'to="frontdoor"' in captured['body']
        assert 'to="parent"' not in captured['body']


class TestWindowFaultTextStopsShortOfConfigWork:
    """`blocking_reason` 是节点读的文案：它可以带观测值，但出路只能是上报。"""

    def test_escalation_clause_present_and_config_off_limits(self):
        loop = object.__new__(ReActToolLoop)

        class _Log:
            def max_observed_request_span_tokens(self, task_id, node_id):
                return 262_144

            def list_task_node_error_logs(self, task_id, node_id):
                return []

        loop._log_service = _Log()  # type: ignore[attr-defined]
        result = loop._window_integrity_failure(
            task_id=TASK,
            node_id=LEAF,
            shape_reason='reply carried no tool call and no text (reasoning-only) [window-clamped]',
            model_key='sensenova-6.8-flash-lite',
            response=SimpleNamespace(
                provider_request_body={'model': 'x', 'max_tokens': 65536},
                usage={'output_tokens': 2255},
            ),
        )
        text = str(result.blocking_reason or '')
        assert 'window_integrity_fault:' in text
        assert '262144' in text
        assert 'Nodes do not manage model chains or runtime configuration' in text
        assert 'send_message(to="frontdoor"' in text


def test_hint_and_receipt_copy_has_no_cjk():
    # 模型可见文案按项目口径写英文（邻居的中文句子不改）。
    samples = [
        error_escalation_hint(receiver_is_root=True),
        error_escalation_hint(receiver_is_root=False),
    ]
    for text in samples:
        assert not any('一' <= ch <= '鿿' for ch in text), json.dumps(text[:80])
