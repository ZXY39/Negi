"""``g3ku mcp`` CLI 测试（typer CliRunner，模式抄 test_external_cli_status）。

核心契约：stdio purity——serve 路径绝不向 stdout 写任何字节（F10），
一切提示走 stderr；``server.run(transport="stdio")`` 是唯一协议入口。
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from g3ku.cli.commands import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def stub_server(monkeypatch):
    """Capture build_mcp_server(client) and stub FastMCP.run."""
    captured: dict = {}

    class _StubMcp:
        def run(self, transport: str = "stdio") -> None:
            captured["transport"] = transport

    def _fake_build(client, *, name: str = "g3ku"):
        captured["client"] = client
        captured["name"] = name
        return _StubMcp()

    monkeypatch.setattr("g3ku.mcp_gateway.server.build_mcp_server", _fake_build)
    return captured


def test_serve_requires_token_and_keeps_stdout_clean(runner: CliRunner):
    result = runner.invoke(app, ["mcp", "serve"], env={"G3KU_EXTERNAL_TOKEN": None})
    assert result.exit_code == 2
    assert result.stdout == ""  # F10：stdout 一个字节都不能有
    assert "token required" in result.stderr


def test_serve_uses_env_token_and_stdio_transport(runner: CliRunner, stub_server):
    result = runner.invoke(app, ["mcp", "serve"], env={"G3KU_EXTERNAL_TOKEN": "env-token"})
    assert result.exit_code == 0, result.stderr
    assert result.stdout == ""  # stdio 协议通道必须纯净
    assert stub_server["transport"] == "stdio"
    client = stub_server["client"]
    assert client.token == "env-token"
    assert client.base_url == "http://127.0.0.1:18790/api/v1"
    assert client.conversation_prefix == "mcp"
    assert "serving on stdio" in result.stderr


def test_serve_options_land_on_client(runner: CliRunner, stub_server):
    result = runner.invoke(
        app,
        [
            "mcp",
            "serve",
            "--token",
            "cli-token",
            "--base-url",
            "http://10.0.0.5:9999/api/v1",
            "--conversation-prefix",
            "agentx",
        ],
    )
    assert result.exit_code == 0, result.stderr
    client = stub_server["client"]
    assert client.token == "cli-token"
    assert client.base_url == "http://10.0.0.5:9999/api/v1"
    assert client.conversation_prefix == "agentx"
    assert result.stdout == ""


def test_check_reports_bridge_id(runner: CliRunner, monkeypatch):
    class _FakeClient:
        def __init__(self, base_url, token, **kwargs):
            self.base_url = base_url
            self.token = token

        async def list_sessions(self):
            return {"ok": True, "bridge_id": "claude-code", "items": [{"session_id": "s1"}]}

        async def aclose(self):
            return None

    monkeypatch.setattr("g3ku.mcp_gateway.client.G3kuMcpClient", _FakeClient)
    result = runner.invoke(app, ["mcp", "check", "--token", "t"])
    assert result.exit_code == 0, result.output
    assert "bridge_id=claude-code" in result.output
    assert "sessions=1" in result.output


def test_check_reports_failure(runner: CliRunner, monkeypatch):
    class _FailingClient:
        def __init__(self, base_url, token, **kwargs):
            pass

        async def list_sessions(self):
            raise RuntimeError("connection refused")

        async def aclose(self):
            return None

    monkeypatch.setattr("g3ku.mcp_gateway.client.G3kuMcpClient", _FailingClient)
    result = runner.invoke(app, ["mcp", "check", "--token", "t"])
    assert result.exit_code == 1
    assert "failed" in result.output
