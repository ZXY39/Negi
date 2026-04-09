from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import main.service.runtime_service as runtime_service_module
from g3ku.config.schema import Config
from main.runtime.node_runner import NodeRunner
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def test_main_runtime_service_normalizes_default_iteration_sentinels(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
    )

    assert service._react_loop._max_iterations == 16
    assert service.node_runner._execution_max_iterations == 16
    assert service.node_runner._acceptance_max_iterations == 16


def test_main_runtime_service_initializes_role_tool_concurrency_from_config(tmp_path: Path):
    config = Config()
    config.agents.role_concurrency.execution = 3
    config.agents.role_concurrency.inspection = 2
    config.agents.node_parallelism.max_parallel_tool_calls_per_node = 7

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        app_config=config,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
    )

    assert service.node_runner._execution_max_concurrency == 3
    assert service.node_runner._acceptance_max_concurrency == 2
    assert service._react_loop._max_parallel_tool_calls == 7


def test_main_runtime_service_refresh_updates_role_tool_concurrency(monkeypatch, tmp_path: Path):
    initial = Config()
    refreshed = Config()
    refreshed.agents.role_concurrency.execution = 4
    refreshed.agents.role_concurrency.inspection = 1
    refreshed.agents.node_parallelism.max_parallel_tool_calls_per_node = 6

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        app_config=initial,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
    )

    monkeypatch.setattr(
        runtime_service_module,
        "get_runtime_config",
        lambda force=False: (refreshed, 42, True),
    )

    changed = service.ensure_runtime_config_current(force=False, reason="test")

    assert changed is True
    assert service.node_runner._execution_max_concurrency == 4
    assert service.node_runner._acceptance_max_concurrency == 1
    assert service._react_loop._max_parallel_tool_calls == 6


def test_main_runtime_service_defaults_event_history_to_store_sibling_with_config(tmp_path: Path):
    config = Config()

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        app_config=config,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
    )

    assert Path(service.store._event_history_dir) == tmp_path / "event-history"


def test_main_runtime_service_honors_explicit_event_history_dir_from_config(tmp_path: Path):
    config = Config()
    config.main_runtime.event_history.dir = str(tmp_path / "history-explicit")

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        app_config=config,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
    )

    assert Path(service.store._event_history_dir) == tmp_path / "history-explicit"


def test_node_runner_uses_min_of_role_and_global_tool_parallel_limits() -> None:
    runner = NodeRunner(
        store=SimpleNamespace(),
        log_service=SimpleNamespace(),
        react_loop=SimpleNamespace(_max_parallel_tool_calls=5, _adaptive_tool_budget_controller=None),
        tool_provider=lambda _node: {},
        execution_model_refs=["execution"],
        acceptance_model_refs=["inspection"],
        execution_max_concurrency=3,
        acceptance_max_concurrency=9,
        max_parallel_child_pipelines=11,
    )

    execution_node = SimpleNamespace(node_kind="execution")
    acceptance_node = SimpleNamespace(node_kind="acceptance")

    assert runner._max_parallel_tool_calls_for(execution_node) == 3
    assert runner._max_parallel_tool_calls_for(acceptance_node) == 5


def test_node_runner_child_pipeline_limit_does_not_reuse_role_tool_concurrency() -> None:
    runner = NodeRunner(
        store=SimpleNamespace(),
        log_service=SimpleNamespace(),
        react_loop=SimpleNamespace(_max_parallel_tool_calls=5, _adaptive_tool_budget_controller=None),
        tool_provider=lambda _node: {},
        execution_model_refs=["execution"],
        acceptance_model_refs=["inspection"],
        execution_max_concurrency=2,
        acceptance_max_concurrency=1,
        max_parallel_child_pipelines=7,
    )

    execution_node = SimpleNamespace(node_kind="execution")
    acceptance_node = SimpleNamespace(node_kind="acceptance")

    assert runner._max_parallel_child_pipelines_for(execution_node) == 7
    assert runner._max_parallel_child_pipelines_for(acceptance_node) == 7
