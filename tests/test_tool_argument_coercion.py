"""Schema-driven repair of container arguments that arrive as their own JSON text.

Some providers hand back an ``array``/``object`` parameter as the serialized text of that
value inside a string. The measured shape is ``send_message.to = '["node:1f3dd7db87e5"]'``
(task:4555863a6a49, 24 calls, all rejected as ``target_missing`` while the identical
argument from the other route group landed 236/236). These tests pin both halves: what
gets repaired, and what must pass through untouched so the rejection lane can still name
the real reason.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.json_schema_utils import coerce_arguments_to_schema, schema_type_names
from g3ku.runtime.tool_impulse import ARGUMENT_COERCION_FIELD
from main.runtime.internal_tools import SendMessageTool
from main.runtime.tool_call_repair import _coerce_xml_parameter_value


class _ProbeTool(Tool):
    """一个参数是数组、一个是联合、一个是纯字符串，且 model schema 与权威 schema 不同形。"""

    @property
    def name(self) -> str:
        return 'probe_tool'

    @property
    def description(self) -> str:
        return 'probe'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'ids': {'type': 'array', 'items': {'type': 'string'}},
                'to': {'type': ['string', 'array'], 'items': {'type': 'string'}},
                'body': {'type': 'string'},
                'draft': {'type': 'object'},
            },
            'required': ['ids'],
        }

    @property
    def model_parameters(self) -> dict[str, Any]:
        # 模型可见面把 ids 的声明改了：剥壳只认 `parameters`，不被投影带跑。
        return {
            'type': 'object',
            'properties': {
                'ids': {'type': 'string'},
            },
        }

    async def execute(self, **kwargs: Any) -> Any:
        return kwargs


def _schema() -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'to': {'type': ['string', 'array'], 'items': {'type': 'string'}},
            'ids': {'type': 'array', 'items': {'type': 'string'}},
            'draft': {'type': 'object'},
            'body': {'type': 'string'},
            'count': {'type': 'integer'},
        },
    }


class TestSchemaTypeNames:
    def test_union_is_expanded(self):
        assert schema_type_names({'type': ['string', 'array']}) == ['string', 'array']

    def test_single_string_spelling(self):
        assert schema_type_names({'type': 'array'}) == ['array']

    def test_missing_or_bad_schema(self):
        assert schema_type_names({}) == []
        assert schema_type_names(None) == []


class TestCoerceArgumentsToSchema:
    def test_union_array_text_becomes_list(self):
        repaired, coerced = coerce_arguments_to_schema(
            _schema(), {'to': '["node:a","node:b"]', 'body': 'hi'}
        )
        assert repaired['to'] == ['node:a', 'node:b']
        assert coerced == ['to']

    def test_declared_array_text_becomes_list(self):
        repaired, coerced = coerce_arguments_to_schema(_schema(), {'ids': '["a"]'})
        assert repaired['ids'] == ['a']
        assert coerced == ['ids']

    def test_declared_object_text_becomes_dict(self):
        repaired, coerced = coerce_arguments_to_schema(_schema(), {'draft': '{"a": 1}'})
        assert repaired['draft'] == {'a': 1}
        assert coerced == ['draft']

    def test_string_declared_is_never_touched_even_when_it_parses(self):
        # 误伤轴：正文本来就允许长得像 JSON。声明不是容器就不许动。
        repaired, coerced = coerce_arguments_to_schema(_schema(), {'body': '[1, 2, 3]'})
        assert repaired['body'] == '[1, 2, 3]'
        assert coerced == []

    def test_wrong_container_kind_is_left_alone(self):
        repaired, coerced = coerce_arguments_to_schema(_schema(), {'ids': '{"a": 1}'})
        assert repaired['ids'] == '{"a": 1}'
        assert coerced == []

    def test_truncated_container_is_not_repaired(self):
        # 严格解析：截断的半截数组绝不许被"补全"成容器再执行。
        repaired, coerced = coerce_arguments_to_schema(_schema(), {'to': '["node:a"'})
        assert repaired['to'] == '["node:a"'
        assert coerced == []

    def test_scalar_union_is_not_repaired_by_the_container_branch(self):
        schema = {'properties': {'count': {'type': ['integer', 'string']}}}
        repaired, coerced = coerce_arguments_to_schema(schema, {'count': '5'})
        assert repaired['count'] == '5'
        assert coerced == []

    def test_undeclared_argument_name_is_left_alone(self):
        repaired, coerced = coerce_arguments_to_schema(_schema(), {'surprise': '["a"]'})
        assert repaired['surprise'] == '["a"]'
        assert coerced == []

    def test_native_containers_pass_through(self):
        repaired, coerced = coerce_arguments_to_schema(_schema(), {'to': ['node:a'], 'ids': []})
        assert repaired == {'to': ['node:a'], 'ids': []}
        assert coerced == []

    def test_bad_inputs(self):
        assert coerce_arguments_to_schema(_schema(), None) == ({}, [])
        assert coerce_arguments_to_schema(None, {'to': '["a"]'}) == ({'to': '["a"]'}, [])
        assert coerce_arguments_to_schema({'type': 'object'}, {'to': '["a"]'}) == (
            {'to': '["a"]'},
            [],
        )


class TestToolCoerceEntrypoint:
    def test_coerce_uses_authoritative_parameters_not_model_projection(self):
        tool = _ProbeTool()
        repaired, coerced = tool.coerce_arguments({'ids': '["a", "b"]'})
        assert repaired['ids'] == ['a', 'b']
        assert coerced == ['ids']

    def test_repaired_arguments_satisfy_validation(self):
        tool = _ProbeTool()
        raw = {'ids': '["a"]'}
        repaired, _ = tool.coerce_arguments(raw)
        assert tool.validate_params(repaired) == []
        # 原文按权威 schema 是不合规的——这正是"验的形状"必须与"跑的形状"同源的理由。
        assert tool.validate_params(raw) != []

    def test_original_dict_is_not_mutated(self):
        tool = _ProbeTool()
        raw = {'ids': '["a"]'}
        tool.coerce_arguments(raw)
        assert raw == {'ids': '["a"]'}


class TestXmlParameterLaneGetsUnionSupport:
    def test_union_array_text_coerces(self):
        value, issue = _coerce_xml_parameter_value(
            '["node:a"]', schema={'type': ['string', 'array'], 'items': {'type': 'string'}}
        )
        assert value == ['node:a']
        assert issue == ''

    def test_array_only_still_reports_when_unparsable(self):
        value, issue = _coerce_xml_parameter_value('not json', schema={'type': 'array'})
        assert value is None
        assert issue == 'must be a valid JSON array'

    def test_union_with_string_falls_back_to_raw_text(self):
        value, issue = _coerce_xml_parameter_value(
            'plain prose', schema={'type': ['string', 'array'], 'items': {'type': 'string'}}
        )
        assert value == 'plain prose'
        assert issue == ''

    def test_single_scalar_types_keep_their_old_verdicts(self):
        assert _coerce_xml_parameter_value('3', schema={'type': 'integer'}) == (3, '')
        assert _coerce_xml_parameter_value('x', schema={'type': 'integer'}) == (
            None,
            'must be an integer',
        )
        assert _coerce_xml_parameter_value('yes', schema={'type': 'boolean'}) == (
            None,
            'must be true or false',
        )


class TestCoercionIsCountable:
    """剥壳对模型不可见，所以落点必须自己带：按 call id 记，且不外泄给工具。"""

    def _loop(self):
        from main.runtime.react_loop import ReActToolLoop

        return object.__new__(ReActToolLoop)

    def test_recorder_keys_by_call_id(self):
        loop = self._loop()
        context: dict[str, Any] = {'current_tool_call_id': 'call_1'}
        loop._record_tool_argument_coercion(context, ['to'])
        assert context['tool_argument_coercions'] == {'call_1': ['to']}

    def test_recorder_ignores_calls_without_an_id_or_a_repair(self):
        loop = self._loop()
        no_id: dict[str, Any] = {}
        loop._record_tool_argument_coercion(no_id, ['to'])
        assert no_id == {}
        no_repair: dict[str, Any] = {'current_tool_call_id': 'call_1'}
        loop._record_tool_argument_coercion(no_repair, [])
        assert 'tool_argument_coercions' not in no_repair

    def test_side_table_never_reaches_the_tool_visible_runtime(self):
        from g3ku.runtime.tool_impulse import tool_facing_runtime_context

        facing = tool_facing_runtime_context({'tool_argument_coercions': {'c': ['to']}, 'task_id': 't'})
        assert 'tool_argument_coercions' not in facing
        assert facing['task_id'] == 't'


class TestCoercionRowPersistence:
    """`arguments_coerced` 是这一刀唯一的计数器，它必须真落到工具行上。"""

    def _service(self, captured: list):
        from unittest.mock import MagicMock

        from main.models import NodeRecord, TaskRecord
        from main.monitoring.log_service import TaskLogService

        stamp = '2026-10-25T20:00:00+08:00'
        task = TaskRecord(
            task_id='task:coerce',
            title='coercion row',
            user_request='coercion row',
            root_node_id='node:a',
            created_at=stamp,
            updated_at=stamp,
        )
        node = NodeRecord(
            node_id='node:a',
            task_id='task:coerce',
            root_node_id='node:a',
            goal='coerce',
            prompt='coerce',
            created_at=stamp,
            updated_at=stamp,
        )
        store = MagicMock()
        store.get_task.return_value = task
        store.get_node.return_value = node
        store.list_task_node_tool_results.return_value = []
        store.upsert_task_node_tool_result.side_effect = lambda record: captured.append(record)
        return TaskLogService(store=store, file_store=MagicMock())

    def _batch(self, live_state: dict[str, Any]):
        from types import SimpleNamespace

        call = SimpleNamespace(
            id='call_1',
            name='send_message',
            # 模型那条原文：数组的 JSON 文本。行上的 `arguments_text` 记的就是它，不被剥壳覆盖。
            arguments={'to': '["node:1f3dd7db87e5"]', 'content': '早上好'},
        )
        results = [{
            'tool_message': {
                'role': 'tool',
                'tool_call_id': 'call_1',
                'name': 'send_message',
                'content': '{"ok": true}',
            },
            'live_state': live_state,
        }]
        return call, results

    def test_arguments_coerced_lands_in_the_row_payload(self):
        captured: list[Any] = []
        service = self._service(captured)
        call, results = self._batch({
            'tool_call_id': 'call_1',
            'tool_name': 'send_message',
            'status': 'success',
            ARGUMENT_COERCION_FIELD: ['to'],
        })
        service.record_tool_result_batch(
            task_id='task:coerce', node_id='node:a', response_tool_calls=[call], results=results
        )
        payload = captured[0].payload
        assert payload[ARGUMENT_COERCION_FIELD] == ['to']
        assert payload['execution_path'] == 'normal'
        # 原文照旧记在 arguments_text 里：形状被修过与模型发了什么是两件事，台账各记一件。
        recorded = json.loads(captured[0].arguments_text)
        assert isinstance(recorded['to'], str) and recorded['to'].startswith('[')

    def test_unrepaired_call_writes_no_key(self):
        captured: list[Any] = []
        service = self._service(captured)
        call, results = self._batch({
            'tool_call_id': 'call_1',
            'tool_name': 'send_message',
            'status': 'success',
        })
        service.record_tool_result_batch(
            task_id='task:coerce', node_id='node:a', response_tool_calls=[call], results=results
        )
        assert ARGUMENT_COERCION_FIELD not in captured[0].payload


class TestLiveFailureShapeRepaired:
    """把实盘那 24 条的到达形状原样喂进口，再交给真工具执行。"""

    def test_recorded_stringified_to_reaches_the_delivery_callback_as_a_list(self):
        recorded: list[dict] = []

        async def deliver(**payload):
            recorded.append(payload)
            return {'ok': True, 'results': [{'target': 'node:a', 'status': 'delivered'}]}

        tool = SendMessageTool(deliver)
        arguments = {
            'content': '千反田同学，早上好。',
            'to': '["node:1f3dd7db87e5"]',
        }
        repaired, coerced = tool.coerce_arguments(arguments)
        assert coerced == ['to']
        assert tool.validate_params(repaired) == []
        asyncio.run(tool.execute(**repaired))
        assert recorded[0]['to'] == ['node:1f3dd7db87e5']
        assert recorded[0]['content'] == '千反田同学，早上好。'
