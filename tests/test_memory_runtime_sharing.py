from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.providers.responses_protocol_helpers import _convert_messages
from g3ku.resources import ResourceManager
from g3ku.runtime.bootstrap_bridge import RuntimeBootstrapBridge
from g3ku.security import get_bootstrap_security_service


class _AsyncCloseSpy:
    def __init__(self):
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


def test_init_main_runtime_binds_configured_paths(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeMainRuntimeService:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    loop = SimpleNamespace(
        resource_manager=None,
        app_config=SimpleNamespace(
            main_runtime=SimpleNamespace(
                store_path=str(tmp_path / 'runtime.sqlite3'),
                files_base_dir=str(tmp_path / 'tasks'),
                artifact_dir=str(tmp_path / 'artifacts'),
                governance_store_path=str(tmp_path / 'governance.sqlite3'),
                default_max_depth=2,
                hard_max_depth=5,
            ),
            get_role_model_keys=lambda role: [f'{role}_model'],
        ),
        main_task_service=None,
    )

    monkeypatch.setattr('g3ku.runtime.bootstrap_bridge.ConfigChatBackend', lambda config: f'backend:{config!r}')
    monkeypatch.setattr('g3ku.runtime.bootstrap_bridge.MainRuntimeService', FakeMainRuntimeService)

    RuntimeBootstrapBridge(loop).init_main_runtime()

    assert captured['store_path'] == str(tmp_path / 'runtime.sqlite3')
    assert captured['files_base_dir'] == str(tmp_path / 'tasks')
    assert captured['artifact_dir'] == str(tmp_path / 'artifacts')
    assert captured['governance_store_path'] == str(tmp_path / 'governance.sqlite3')
    assert captured['execution_model_refs'] == ['execution_model']
    assert captured['acceptance_model_refs'] == ['inspection_model']


@pytest.mark.asyncio
async def test_close_mcp_closes_main_task_service_only():
    from g3ku.runtime.engine import AgentRuntimeEngine

    engine = AgentRuntimeEngine.__new__(AgentRuntimeEngine)
    engine._runtime_closed = False
    engine._consolidation_tasks = set()
    engine._commit_tasks = set()
    engine.background_pool = None
    engine.main_task_service = _AsyncCloseSpy()
    engine.memory_manager = None

    await AgentRuntimeEngine.close_mcp(engine)

    assert engine.main_task_service.closed == 1


def test_convert_messages_strips_dangling_assistant_tool_calls():
    system_prompt, input_items = _convert_messages(
        [
            {"role": "system", "content": "sys"},
            {
                "role": "assistant",
                "content": "I will search.",
                "tool_calls": [
                    {
                        "id": "call_dangling|fc_deadbeef",
                        "type": "tool_call",
                        "function": {"name": "web_fetch", "arguments": '{"url":"https://example.com"}'},
                    }
                ],
            },
            {"role": "user", "content": "继续"},
        ]
    )

    assert system_prompt == "sys"
    assert input_items == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I will search."}],
            "status": "completed",
            "id": "msg_1",
        },
        {"role": "user", "content": [{"type": "input_text", "text": "继续"}]},
    ]


def test_convert_messages_keeps_completed_tool_calls_and_outputs():
    system_prompt, input_items = _convert_messages(
        [
            {"role": "system", "content": "sys"},
            {
                "role": "assistant",
                "content": "Fetching now.",
                "tool_calls": [
                    {
                        "id": "call_ok|fc_good",
                        "type": "tool_call",
                        "function": {"name": "web_fetch", "arguments": '{"url":"https://example.com"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "web_fetch",
                "tool_call_id": "call_ok|fc_good",
                "content": '{"status":"ok"}',
            },
            {"role": "user", "content": "总结一下"},
        ]
    )

    assert system_prompt == "sys"
    assert input_items[0]["type"] == "message"
    assert input_items[1] == {
        "type": "function_call",
        "id": "fc_good",
        "call_id": "call_ok",
        "name": "web_fetch",
        "arguments": '{"url":"https://example.com"}',
    }
    assert input_items[2] == {
        "type": "function_call_output",
        "call_id": "call_ok",
        "output": [{"type": "input_text", "text": '{"status":"ok"}'}],
    }
    assert input_items[3] == {"role": "user", "content": [{"type": "input_text", "text": "总结一下"}]}


def test_convert_messages_merges_multiple_system_messages_in_order():
    # 回归（阶段块 assistant→system 角色对齐的硬前置）：历史里可以同时存在多条
    # system 消息——基础系统提示、mid-history 的运行时工具契约、[G3KU_STAGE_*]
    # 阶段压缩块。旧实现 `system_prompt = content` 后写覆盖前者：只剩最后一条
    # system，基础系统提示与其余全部块被静默丢弃。现在必须按序合并、不丢失、
    # 不覆盖。
    system_prompt, input_items = _convert_messages(
        [
            {"role": "system", "content": "BASE PROMPT"},
            {"role": "user", "content": "hello"},
            {"role": "system", "content": '[G3KU_STAGE_COMPACT_V1]\n{"stage_index":1}'},
            {"role": "assistant", "content": "ok"},
            {
                "role": "system",
                "content": "## Runtime Tool Contract\nkind: frontdoor_runtime_tool_contract",
            },
            {"role": "user", "content": "continue"},
        ]
    )

    # 三段 system 内容全部保留，且维持历史顺序
    assert "BASE PROMPT" in system_prompt
    assert '[G3KU_STAGE_COMPACT_V1]\n{"stage_index":1}' in system_prompt
    assert "## Runtime Tool Contract" in system_prompt
    assert (
        system_prompt.index("BASE PROMPT")
        < system_prompt.index("[G3KU_STAGE_COMPACT_V1]")
        < system_prompt.index("## Runtime Tool Contract")
    )
    # 非 system 消息按序转换进 input_items，不受合并影响
    assert [item.get("role") for item in input_items] == ["user", "assistant", "user"]


def test_convert_messages_keeps_every_stage_block_from_many_system_messages():
    # 事故会话里有 43 个阶段压缩块：改 system 角色后走 Responses provider 时，
    # 每一个块都必须出现在合并后的 system prompt 里，一个都不能被覆盖丢失。
    blocks = [
        f'[G3KU_STAGE_COMPACT_V1]\n{{"stage_index":{index},"completed_stage_summary":"s{index}"}}'
        for index in range(1, 44)
    ]
    messages: list[dict] = [{"role": "system", "content": "BASE PROMPT"}]
    for index, block in enumerate(blocks):
        messages.append({"role": "user", "content": f"u{index}"})
        messages.append({"role": "assistant", "content": f"a{index}"})
        messages.append({"role": "system", "content": block})

    system_prompt, input_items = _convert_messages(messages)

    assert "BASE PROMPT" in system_prompt
    for block in blocks:
        assert block in system_prompt
    # user/assistant 消息逐条保留（43 组 × 2）
    assert len(input_items) == 86
    assert all(item.get("role") in {"user", "assistant"} for item in input_items)


def test_convert_messages_skips_empty_system_messages_when_merging():
    system_prompt, _input_items = _convert_messages(
        [
            {"role": "system", "content": "BASE"},
            {"role": "system", "content": ""},
            {"role": "system", "content": "   "},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "TAIL CONTRACT"},
        ]
    )
    assert system_prompt == "BASE\n\nTAIL CONTRACT"


def test_sync_internal_tool_runtimes_reads_memory_runtime_manifest(tmp_path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(__file__).resolve().parents[1] / 'tools' / 'memory_runtime', workspace / 'tools' / 'memory_runtime')

    manager = ResourceManager(
        workspace,
        app_config=SimpleNamespace(
            resources=SimpleNamespace(
                enabled=True,
                skills_dir='skills',
                tools_dir='tools',
                manifest_name='resource.yaml',
                state_path='.g3ku/resources.state.json',
                reload=SimpleNamespace(enabled=True, poll_interval_ms=200, debounce_ms=100, lazy_reload_on_access=True, keep_last_good_version=True),
                locks=SimpleNamespace(lock_dir='.g3ku/resource-locks', logical_delete_guard=True, windows_fs_lock=True),
            )
        ),
    )
    manager.reload_now(trigger='test-bind')

    class _FakeMemoryManager:
        def __init__(self, workspace_path, cfg):
            self.workspace = workspace_path
            self.cfg = cfg
            self.store = object()
            self.closed = 0

        def close(self):
            self.closed += 1

    loop = SimpleNamespace(
        workspace=workspace,
        resource_manager=manager,
        _internal_tool_settings_fingerprints={},
        _memory_manager_cls=_FakeMemoryManager,
        memory_manager=None,
        commit_service=None,
        _memory_runtime_settings=None,
    )

    try:
        changed = RuntimeBootstrapBridge(loop).sync_internal_tool_runtimes(force=True, reason='test')
        assert changed is True
        assert loop._memory_runtime_settings is not None
        assert loop._memory_runtime_settings.enabled is True
        assert loop._memory_runtime_settings.document.summary_max_chars == 300
        assert loop._memory_runtime_settings.document.document_max_chars == 20000
        assert loop._memory_runtime_settings.queue.batch_max_chars == 50000
        assert loop._memory_runtime_settings.queue.max_wait_seconds == 3
        assert loop.memory_manager is not None
    finally:
        manager.close()


