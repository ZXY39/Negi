"""CEO 前门会话级临时目录（temp/ceo/<session>）的注入与契约渲染回归。

背景：web CEO 会话此前没有 task_temp_dir，模型经 exec 重定向落盘的临时文件
散落在工作区根目录（如 `.tmp_skills_*.txt`）。本文件覆盖三件事：
1. `ceo_session_temp_dir` 的路径解析与会话键规范化；
2. `_build_tool_runtime_context` 注入 `task_temp_dir`（exec 默认 cwd 的数据源）；
3. 运行时工具契约渲染 `session_temp_dir:` 行，供模型获知绝对落点。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from g3ku.runtime.frontdoor.session_temp_dir import ceo_session_temp_dir
from g3ku.runtime.frontdoor.tool_contract import (
    build_frontdoor_tool_contract,
    frontdoor_tool_contract_payload_from_message,
)

SESSION_KEY = 'web:ceo-57836448a1d7'


def test_ceo_session_temp_dir_sanitizes_session_key(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()

    result = ceo_session_temp_dir(workspace, SESSION_KEY)

    assert result == str(workspace / 'temp' / 'ceo' / 'web_ceo-57836448a1d7')


def test_ceo_session_temp_dir_falls_back_to_shared(tmp_path: Path) -> None:
    result = ceo_session_temp_dir(tmp_path, '')

    assert result == str(tmp_path / 'temp' / 'ceo' / 'shared')


def test_frontdoor_contract_renders_session_temp_dir(tmp_path: Path) -> None:
    session_temp = str(tmp_path / 'temp' / 'ceo' / 'web_ceo-abc')
    contract = build_frontdoor_tool_contract(
        callable_tool_names=['exec'],
        candidate_tool_names=[],
        hydrated_tool_names=[],
        frontdoor_stage_state={},
        session_temp_dir=session_temp,
    )

    message = contract.to_message()

    assert f'session_temp_dir: {session_temp}' in str(message['content'])
    payload = frontdoor_tool_contract_payload_from_message(message)
    assert payload is not None
    assert payload.get('session_temp_dir') == session_temp


def test_frontdoor_contract_omits_session_temp_dir_when_absent() -> None:
    contract = build_frontdoor_tool_contract(
        callable_tool_names=['exec'],
        candidate_tool_names=[],
        hydrated_tool_names=[],
        frontdoor_stage_state={},
    )

    message = contract.to_message()

    assert 'session_temp_dir:' not in str(message['content'])
    payload = frontdoor_tool_contract_payload_from_message(message)
    assert payload is not None
    assert payload.get('session_temp_dir') is None


def test_ceo_runtime_context_injects_session_task_temp_dir(tmp_path: Path) -> None:
    from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    session = SimpleNamespace(
        state=SimpleNamespace(session_key=SESSION_KEY),
        _channel='web',
        _chat_id=SESSION_KEY,
        _memory_channel='web',
        _memory_chat_id=SESSION_KEY,
        _active_cancel_token=None,
        inflight_turn_snapshot=None,
        _current_turn_id=lambda: 'turn-1',
    )
    loop = SimpleNamespace(
        workspace=workspace,
        temp_dir=str(workspace / '.g3ku' / 'tmp'),
        sessions=SimpleNamespace(
            get_or_create=lambda key: SimpleNamespace(state=SimpleNamespace(session_key=key)),
        ),
    )
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = loop
    ops._resolve_ceo_model_refs = lambda: []
    ops._ceo_image_multimodal_enabled_for_model_refs = lambda refs: False
    ops._session_task_defaults = lambda record: {}
    runtime = SimpleNamespace(context=SimpleNamespace(session=session, on_progress=None))

    context = ops._build_tool_runtime_context(state={}, runtime=runtime)

    assert context['task_temp_dir'] == str(workspace / 'temp' / 'ceo' / 'web_ceo-57836448a1d7')
