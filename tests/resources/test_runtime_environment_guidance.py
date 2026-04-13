from __future__ import annotations

import os
from types import SimpleNamespace

import g3ku.runtime.frontdoor.prompt_builder as prompt_builder_module
import main.runtime.node_runner as node_runner_module
from g3ku.runtime.frontdoor.prompt_builder import CeoPromptBuilder
from main.runtime.node_runner import NodeRunner


def _fake_project_environment() -> dict[str, object]:
    return {
        'shell_family': 'powershell',
        'process_cwd': r'D:\projects\G3KU',
        'workspace_root': r'D:\projects\G3KU',
        'project_python': r'C:\Python314\python.exe',
        'project_python_dir': r'C:\Python314',
        'project_scripts_dir': r'C:\Python314\Scripts',
        'project_path_entries': [r'C:\Python314', r'C:\Python314\Scripts'],
        'project_virtual_env': r'C:\Python314',
        'project_python_hint': r"& 'C:\Python314\python.exe'",
    }


def test_ceo_prompt_builder_mentions_project_python_guidance(monkeypatch) -> None:
    monkeypatch.setattr(prompt_builder_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())

    prompt = CeoPromptBuilder(loop=SimpleNamespace(workspace=r'D:\projects\G3KU')).build(skills=[])

    assert 'G3KU_PROJECT_PYTHON' in prompt
    assert r"& 'C:\Python314\python.exe'" in prompt
    assert '{{project_python_hint}}' not in prompt


def test_ceo_prompt_builder_mentions_task_control_guidance(monkeypatch) -> None:
    monkeypatch.setattr(prompt_builder_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())

    prompt = CeoPromptBuilder(loop=SimpleNamespace(workspace=r'D:\projects\G3KU')).build(skills=[])

    assert 'task_id' in prompt
    assert 'stop_tool_execution' in prompt
    assert 'detached 后台工具协议' in prompt


def test_ceo_prompt_builder_mentions_skill_loading_guidance(monkeypatch) -> None:
    monkeypatch.setattr(prompt_builder_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())

    prompt = CeoPromptBuilder(loop=SimpleNamespace(workspace=r'D:\projects\G3KU')).build(skills=[])

    assert 'load_skill_context' in prompt
    assert 'skill_id' in prompt


def test_ceo_prompt_builder_includes_stage_first_protocol_and_recovery_rule(monkeypatch) -> None:
    monkeypatch.setattr(prompt_builder_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())

    prompt = CeoPromptBuilder(loop=SimpleNamespace(workspace=r'D:\projects\G3KU')).build(skills=[])

    assert '必须先调用`submit_next_stage`工具创建阶段后才能使用工具' in prompt
    assert '如果调用工具返回 `no active stage`，下一步必须立即调用 `submit_next_stage` 进入阶段' in prompt


def test_ceo_prompt_builder_visible_skills_block_requires_active_stage() -> None:
    builder = CeoPromptBuilder(loop=SimpleNamespace(workspace=r'D:\projects\G3KU'))

    block = builder.build_visible_skills_block(
        skills=[
            {
                'skill_id': 'find-skills',
                'display_name': 'find-skills',
                'description': '查找 skill',
                'l0': '查找 skill',
            }
        ]
    )

    assert '## 本轮可见技能' in block
    assert '如果当前还没有活动阶段且你需要使用工具，第一步必须先调用 `submit_next_stage`。' in block
    assert '仅当当前已经存在活动阶段且你确实需要完整工作流正文时' in block
    assert '仅在当前已经存在活动阶段后调用 `load_skill_context(skill_id="find-skills")`。' in block


def test_node_runner_runtime_context_and_prompt_contract_include_project_python(monkeypatch) -> None:
    monkeypatch.setattr(node_runner_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())
    runner = NodeRunner(
        store=None,
        log_service=None,
        react_loop=None,
        tool_provider=None,
        execution_model_refs=['execution_model'],
        acceptance_model_refs=['acceptance_model'],
    )
    task = SimpleNamespace(task_id='task:1', session_id='web:shared', metadata={})
    node = SimpleNamespace(node_id='node:1', depth=0, node_kind='execution', can_spawn_children=False)

    runtime_context = runner._runtime_context(task=task, node=node)
    prompt = runner._build_system_prompt(node=node)

    assert runtime_context['project_python'] == r'C:\Python314\python.exe'
    assert runtime_context['project_python_hint'] == r"& 'C:\Python314\python.exe'"
    assert runtime_context['task_temp_dir'].endswith(r'G3KU\temp')
    assert 'runtime_environment' in prompt
    assert 'project_python_hint' in prompt
    assert 'tool_guidance' in prompt
    assert r"& 'C:\Python314\python.exe'" not in prompt


def test_node_execution_prompt_mentions_visible_skill_only_policy(monkeypatch) -> None:
    monkeypatch.setattr(node_runner_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())
    runner = NodeRunner(
        store=None,
        log_service=None,
        react_loop=None,
        tool_provider=None,
        execution_model_refs=['execution_model'],
        acceptance_model_refs=['acceptance_model'],
    )
    node = SimpleNamespace(node_id='node:1', depth=0, node_kind='execution', can_spawn_children=False)

    prompt = runner._build_system_prompt(node=node)

    assert 'visible_skills' in prompt
    assert 'load_skill_context(skill_id="' in prompt


def test_node_runner_runtime_environment_uses_task_temp_dir_from_runtime_meta(monkeypatch) -> None:
    monkeypatch.setattr(node_runner_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())
    task_temp_dir = r'D:\projects\G3KU\temp\tasks\task_1'
    runner = NodeRunner(
        store=None,
        log_service=SimpleNamespace(read_task_runtime_meta=lambda _task_id: {'task_temp_dir': task_temp_dir}),
        react_loop=None,
        tool_provider=None,
        execution_model_refs=['execution_model'],
        acceptance_model_refs=['acceptance_model'],
    )
    task = SimpleNamespace(task_id='task:1', session_id='web:shared', metadata={})

    payload = runner._runtime_environment_payload(task=task)
    prompt = runner._build_system_prompt(node=SimpleNamespace(node_kind='execution'))

    assert os.path.normcase(payload['task_temp_dir']) == os.path.normcase(task_temp_dir)
    assert payload['path_policy']['exec_default_working_dir'] == 'task_temp_dir'
    assert 'task_temp_dir' in prompt
    assert '所有文件都放在' in prompt


def test_acceptance_prompt_mentions_task_temp_dir_rule(monkeypatch) -> None:
    monkeypatch.setattr(node_runner_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())
    runner = NodeRunner(
        store=None,
        log_service=None,
        react_loop=None,
        tool_provider=None,
        execution_model_refs=['execution_model'],
        acceptance_model_refs=['acceptance_model'],
    )

    prompt = runner._build_system_prompt(node=SimpleNamespace(node_kind='acceptance'))

    assert 'task_temp_dir' in prompt
    assert '所有文件都放在' in prompt


def test_execution_and_acceptance_prompts_forbid_self_task_progress_polling(monkeypatch) -> None:
    monkeypatch.setattr(node_runner_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())
    runner = NodeRunner(
        store=None,
        log_service=None,
        react_loop=None,
        tool_provider=None,
        execution_model_refs=['execution_model'],
        acceptance_model_refs=['acceptance_model'],
    )

    execution_prompt = runner._build_system_prompt(node=SimpleNamespace(node_kind='execution'))
    acceptance_prompt = runner._build_system_prompt(node=SimpleNamespace(node_kind='acceptance'))

    assert '当前正在执行的 `task_id`' in execution_prompt
    assert '不得对当前正在执行的 `task_id` 调用' in execution_prompt
    assert '`task_progress`' in execution_prompt
    assert 'spawn_child_nodes' in execution_prompt
    assert 'content.search' in execution_prompt
    assert 'content.open' in execution_prompt
    assert '不要重复调用完全相同的只读/检索工具' in execution_prompt

    assert '当前正在执行的 `task_id`' in acceptance_prompt
    assert '不得对当前正在执行的 `task_id` 调用 `task_progress`' in acceptance_prompt
    assert 'content.search' in acceptance_prompt
    assert 'content.open' in acceptance_prompt
    assert '不要重复调用完全相同的只读/检索工具' in acceptance_prompt


def test_node_execution_prompt_mentions_spawn_interception_guidance(monkeypatch) -> None:
    monkeypatch.setattr(node_runner_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())
    runner = NodeRunner(
        store=None,
        log_service=None,
        react_loop=None,
        tool_provider=None,
        execution_model_refs=['execution_model'],
        acceptance_model_refs=['acceptance_model'],
    )

    prompt = runner._build_system_prompt(node=SimpleNamespace(node_kind='execution'))

    assert '不合理的派生将被拦截' in prompt
    assert '被拦截时需要参考被拦截的原因和建议' in prompt
