from __future__ import annotations

from pathlib import Path

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
