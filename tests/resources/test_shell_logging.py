from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.shells.cli import run_agent_shell


class _CollectHandler(logging.Handler):
    def __init__(self, sink: list[str]) -> None:
        super().__init__(level=logging.INFO)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(record.getMessage())


class _FakeRuntimeBridge:
    def __init__(self, _runtime_manager) -> None:
        pass

    async def prompt(self, *_args, **_kwargs):
        return SimpleNamespace(output="ok")


class _FakeRuntimeManager:
    def __init__(self, loop) -> None:
        self.loop = loop


class _FakeAgentLoop:
    channels_config = None

    async def close_mcp(self) -> None:
        return None


@pytest.fixture
def restore_logging_state():
    root = logging.getLogger()
    openai_logger = logging.getLogger("openai")
    httpx_logger = logging.getLogger("httpx")

    state = {
        "root_handlers": list(root.handlers),
        "root_level": root.level,
        "openai_handlers": list(openai_logger.handlers),
        "openai_level": openai_logger.level,
        "openai_propagate": openai_logger.propagate,
        "httpx_handlers": list(httpx_logger.handlers),
        "httpx_level": httpx_logger.level,
        "httpx_propagate": httpx_logger.propagate,
    }

    yield

    root.handlers = state["root_handlers"]
    root.setLevel(state["root_level"])
    openai_logger.handlers = state["openai_handlers"]
    openai_logger.setLevel(state["openai_level"])
    openai_logger.propagate = state["openai_propagate"]
    httpx_logger.handlers = state["httpx_handlers"]
    httpx_logger.setLevel(state["httpx_level"])
    httpx_logger.propagate = state["httpx_propagate"]


def test_run_agent_shell_routes_openai_logs_to_plain_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    restore_logging_state,
) -> None:
    root = logging.getLogger()
    leaked_messages: list[str] = []
    root.handlers = [_CollectHandler(leaked_messages)]
    root.setLevel(logging.INFO)

    openai_logger = logging.getLogger("openai")
    openai_logger.handlers = []
    openai_logger.setLevel(logging.INFO)
    openai_logger.propagate = True

    httpx_logger = logging.getLogger("httpx")
    httpx_logger.handlers = []
    httpx_logger.setLevel(logging.INFO)
    httpx_logger.propagate = True

    monkeypatch.setenv("OPENAI_LOG", "info")

    import g3ku.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "SessionRuntimeBridge", _FakeRuntimeBridge)
    monkeypatch.setattr(runtime_module, "SessionRuntimeManager", _FakeRuntimeManager)
    monkeypatch.setattr(runtime_module, "cli_event_text", lambda _event: ("progress", ""))

    run_agent_shell(
        message="hello",
        session_id="cli:test",
        markdown=True,
        logs=True,
        debug=False,
        console=SimpleNamespace(status=lambda *_args, **_kwargs: None),
        logo_text="g3ku",
        load_config=lambda: SimpleNamespace(workspace_path=tmp_path),
        get_data_dir=lambda: tmp_path,
        make_provider=lambda _config: object(),
        make_agent_loop=lambda *_args, **_kwargs: _FakeAgentLoop(),
        set_debug_mode=lambda _enabled: None,
        sync_workspace_templates=lambda _workspace: None,
        init_prompt_session=lambda _workspace: None,
        flush_pending_tty_input=lambda: None,
        restore_terminal=lambda: None,
        read_interactive_input_async=lambda: None,
        is_exit_command=lambda _command: False,
        print_agent_response=lambda _response, _markdown: None,
    )

    logging.getLogger("openai._base_client").info(
        "Retrying request to %s in %f seconds",
        "/chat/completions",
        0.410672,
    )
    captured = capsys.readouterr()

    assert leaked_messages == []
    assert "Retrying request to /chat/completions in 0.410672 seconds" in captured.err
    assert "Retrying request to /chat/completions in 0.410672 seconds\n" in captured.err
