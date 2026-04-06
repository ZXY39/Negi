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


def test_ceo_prompt_builder_mentions_task_id_and_execution_id_guidance(monkeypatch) -> None:
    monkeypatch.setattr(prompt_builder_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())

    prompt = CeoPromptBuilder(loop=SimpleNamespace(workspace=r'D:\projects\G3KU')).build(skills=[])

    assert 'task_id' in prompt
    assert 'execution_id' in prompt
    assert 'stop_tool_execution' in prompt


def test_ceo_prompt_builder_mentions_clawhub_skill_manager(monkeypatch) -> None:
    monkeypatch.setattr(prompt_builder_module, 'current_project_environment', lambda **kwargs: _fake_project_environment())

    prompt = CeoPromptBuilder(loop=SimpleNamespace(workspace=r'D:\projects\G3KU')).build(skills=[])

    assert 'clawhub-skill-manager' in prompt
    assert 'load_skill_context(skill_id="clawhub-skill-manager")' in prompt


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
    assert '不得对当前正在执行的 `task_id` 调用 `task_progress`' in execution_prompt
    assert 'spawn_child_nodes' in execution_prompt
    assert 'content.search' in execution_prompt
    assert 'content.open' in execution_prompt

    assert '当前正在执行的 `task_id`' in acceptance_prompt
    assert '不得对当前正在执行的 `task_id` 调用 `task_progress`' in acceptance_prompt
    assert 'content.search' in acceptance_prompt
    assert 'content.open' in acceptance_prompt
