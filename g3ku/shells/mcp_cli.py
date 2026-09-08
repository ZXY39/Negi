"""MCP gateway CLI shell bindings (``g3ku mcp ...``).

stdout 纯净是 stdio MCP 的铁律：``serve`` 路径的一切提示必须走 stderr
（``typer.echo(..., err=True)``），一个 stdout 字节就会毁掉 JSON-RPC 帧
（loguru 与 FastMCP 日志已验证为 stderr-only）。``check`` 不跑 stdio 协议，
允许正常 console 输出。
"""

from __future__ import annotations

import asyncio
import contextlib

import typer


def build_mcp_app(console) -> typer.Typer:
    app = typer.Typer(help="MCP gateway: expose g3ku to external AI agents over stdio (Model Context Protocol).")

    def _resolve_token(token: str | None) -> str:
        return str(token or "").strip()

    @app.command("serve")
    def serve(
        token: str = typer.Option(
            None,
            "--token",
            envvar="G3KU_EXTERNAL_TOKEN",
            show_default=False,
            help="External Agent API bearer token (externalApi.tokens.*); falls back to env G3KU_EXTERNAL_TOKEN.",
        ),
        base_url: str = typer.Option(
            "http://127.0.0.1:18790/api/v1",
            "--base-url",
            help="External Agent API base URL of the RUNNING g3ku web runtime.",
        ),
        conversation_prefix: str = typer.Option(
            "mcp",
            "--conversation-prefix",
            help="external_key namespace prefix for conversations.",
        ),
    ) -> None:
        """Run the stdio MCP proxy until the client disconnects."""
        resolved = _resolve_token(token)
        if not resolved:
            typer.echo("error: token required (--token or G3KU_EXTERNAL_TOKEN)", err=True)
            raise typer.Exit(2)
        # Lazy imports keep `g3ku --help` light and the mcp SDK off the CLI path.
        from g3ku.mcp_gateway.client import G3kuMcpClient
        from g3ku.mcp_gateway.server import build_mcp_server

        client = G3kuMcpClient(base_url, resolved, conversation_prefix=conversation_prefix)
        server = build_mcp_server(client)
        typer.echo(f"g3ku mcp gateway serving on stdio -> {base_url}", err=True)
        try:
            server.run(transport="stdio")
        except KeyboardInterrupt:
            pass
        finally:
            with contextlib.suppress(Exception):
                asyncio.run(client.aclose())

    @app.command("check")
    def check(
        token: str = typer.Option(
            None,
            "--token",
            envvar="G3KU_EXTERNAL_TOKEN",
            show_default=False,
            help="External Agent API bearer token; falls back to env G3KU_EXTERNAL_TOKEN.",
        ),
        base_url: str = typer.Option(
            "http://127.0.0.1:18790/api/v1",
            "--base-url",
            help="External Agent API base URL of the RUNNING g3ku web runtime.",
        ),
    ) -> None:
        """Connectivity self-check against a running web runtime (GET /sessions)."""
        resolved = _resolve_token(token)
        if not resolved:
            console.print("[red]error: token required (--token or G3KU_EXTERNAL_TOKEN)[/red]")
            raise typer.Exit(2)
        from g3ku.mcp_gateway.client import G3kuMcpClient

        async def _run() -> tuple[bool, str]:
            client = G3kuMcpClient(base_url, resolved)
            try:
                payload = await client.list_sessions()
                bridge_id = str(payload.get("bridge_id") or "")
                count = len(list(payload.get("items") or []))
                return True, f"ok: bridge_id={bridge_id or '(unknown)'} sessions={count} base_url={base_url}"
            except Exception as exc:  # noqa: BLE001 - operator-facing diagnostic
                return False, f"failed: {type(exc).__name__}: {exc}"
            finally:
                await client.aclose()

        ok, message = asyncio.run(_run())
        if ok:
            console.print(f"[green]{message}[/green]")
            return
        console.print(f"[red]{message}[/red]")
        console.print("hint: web 运行时需在跑、externalApi.enabled=true、token 属于启用的条目；423=项目锁定。")
        raise typer.Exit(1)

    return app
