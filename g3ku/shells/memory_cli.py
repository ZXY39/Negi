"""Memory CLI shell bindings for the converged architecture."""

from __future__ import annotations

import asyncio
import json

import typer
from rich.table import Table

import g3ku.agent.rag_memory as rag_memory
import g3ku.config.loader as config_loader


def build_memory_app(console) -> typer.Typer:
    app = typer.Typer(help="Manage RAG memory")
    decay_app = typer.Typer(help="Retention and decay operations")
    pending_app = typer.Typer(help="Manage pending memory facts")
    app.add_typer(decay_app, name="decay")
    app.add_typer(pending_app, name="pending")

    def _load_manager():
        config = config_loader.load_config()
        mem_cfg = config.tools.memory
        return config, mem_cfg, rag_memory.MemoryManager(config.workspace_path, mem_cfg)

    @app.command("stats")
    def memory_stats():
        """Show RAG memory statistics."""
        config, mem_cfg, manager = _load_manager()
        if not mem_cfg.enabled:
            console.print("[yellow]Memory is disabled in config.tools.memory.enabled[/yellow]")
            return
        try:
            stats = asyncio.run(manager.stats())
        finally:
            manager.close()

        table = Table(title="Memory Stats")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        for key in (
            "records",
            "records_v2",
            "pending",
            "records_by_type",
            "layer_distribution",
            "planner_calls",
            "commit_calls",
            "rerank_calls",
            "token_in",
            "token_out",
            "cost_delta_pct",
            "dense_enabled",
            "sqlite_path",
            "qdrant_path",
        ):
            val = stats.get(key)
            if isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False)
            table.add_row(key, str(val))
        console.print(table)

    @app.command("trace")
    def memory_trace(
        session: str = typer.Option(..., "--session", help="Session id (e.g. cli:direct)"),
        limit: int = typer.Option(20, "--limit", "-n", help="Maximum rows"),
    ):
        """Show retrieval traces for a session."""
        config, mem_cfg, manager = _load_manager()
        if not mem_cfg.enabled:
            console.print("[yellow]Memory is disabled in config.tools.memory.enabled[/yellow]")
            return
        try:
            rows = asyncio.run(manager.get_traces(session_key=session, limit=limit))
        finally:
            manager.close()

        if not rows:
            console.print("No traces found.")
            return

        table = Table(title=f"Memory Trace ({session})")
        table.add_column("Trace ID", style="cyan")
        table.add_column("Timestamp")
        table.add_column("Plan")
        table.add_column("Candidates")
        table.add_column("Injected")
        table.add_column("Tokens")
        for row in rows:
            table.add_row(
                str(row.get("trace_id", "")),
                str(row.get("timestamp", "")),
                str(len(row.get("plan", []) or [])),
                str(len(row.get("candidates", []) or [])),
                str(len(row.get("injected_blocks", []) or [])),
                str(row.get("token_budget_used", "")),
            )
        console.print(table)

    @app.command("explain")
    def memory_explain(
        query: str = typer.Option(..., "--query", help="User query text"),
        session: str = typer.Option(..., "--session", help="Session id (e.g. cli:direct)"),
    ):
        """Explain retrieval planning and injection for a query."""
        config, mem_cfg, manager = _load_manager()
        if not mem_cfg.enabled:
            console.print("[yellow]Memory is disabled in config.tools.memory.enabled[/yellow]")
            return

        channel, chat_id = (session.split(":", 1) + [""])[:2] if ":" in session else ("cli", session)
        try:
            result = asyncio.run(
                manager.explain_query(
                    query=query,
                    session_key=session,
                    channel=channel,
                    chat_id=chat_id,
                )
            )
        finally:
            manager.close()

        console.print_json(json.dumps(result, ensure_ascii=False))

    @app.command("migrate-v2")
    def memory_migrate_v2(
        dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing"),
    ):
        """Migrate legacy memory store into V2 records."""
        config, mem_cfg, manager = _load_manager()
        if not mem_cfg.enabled:
            console.print("[yellow]Memory is disabled in config.tools.memory.enabled[/yellow]")
            raise typer.Exit(1)
        try:
            report = asyncio.run(manager.migrate_v2(dry_run=dry_run))
        finally:
            manager.close()

        console.print_json(json.dumps(report, ensure_ascii=False))

    @decay_app.command("run")
    def memory_decay_run(
        dry_run: bool = typer.Option(False, "--dry-run", help="Preview deletions only"),
    ):
        """Execute retention/decay cleanup."""
        config, mem_cfg, manager = _load_manager()
        if not mem_cfg.enabled:
            console.print("[yellow]Memory is disabled in config.tools.memory.enabled[/yellow]")
            raise typer.Exit(1)
        try:
            report = asyncio.run(manager.run_decay(dry_run=dry_run))
        finally:
            manager.close()

        console.print_json(json.dumps(report, ensure_ascii=False))

    @pending_app.command("list")
    def memory_pending_list(limit: int = typer.Option(50, "--limit", "-n", help="Maximum rows")):
        """List pending fact candidates."""
        config, mem_cfg, manager = _load_manager()
        if not mem_cfg.enabled:
            console.print("[yellow]Memory is disabled in config.tools.memory.enabled[/yellow]")
            return
        try:
            rows = asyncio.run(manager.list_pending(limit=limit))
        finally:
            manager.close()

        if not rows:
            console.print("No pending facts.")
            return

        table = Table(title="Pending Facts")
        table.add_column("ID", style="cyan")
        table.add_column("Confidence")
        table.add_column("Reason")
        table.add_column("Preview")
        for row in rows:
            preview = row.candidate.replace("\n", " ")[:120]
            table.add_row(row.pending_id, f"{row.confidence:.2f}", row.reason, preview)
        console.print(table)

    @pending_app.command("approve")
    def memory_pending_approve(pending_id: str = typer.Argument(..., help="Pending fact id")):
        """Approve one pending fact."""
        config, mem_cfg, manager = _load_manager()
        if not mem_cfg.enabled:
            console.print("[yellow]Memory is disabled in config.tools.memory.enabled[/yellow]")
            raise typer.Exit(1)
        try:
            ok = asyncio.run(manager.update_pending(pending_id, "approved"))
        finally:
            manager.close()

        if ok:
            console.print(f"[green]OK[/green] Approved pending fact {pending_id}")
        else:
            console.print(f"[red]Pending fact {pending_id} not found[/red]")
            raise typer.Exit(1)

    @pending_app.command("reject")
    def memory_pending_reject(pending_id: str = typer.Argument(..., help="Pending fact id")):
        """Reject one pending fact."""
        config, mem_cfg, manager = _load_manager()
        if not mem_cfg.enabled:
            console.print("[yellow]Memory is disabled in config.tools.memory.enabled[/yellow]")
            raise typer.Exit(1)
        try:
            ok = asyncio.run(manager.update_pending(pending_id, "rejected"))
        finally:
            manager.close()

        if ok:
            console.print(f"[green]OK[/green] Rejected pending fact {pending_id}")
        else:
            console.print(f"[red]Pending fact {pending_id} not found[/red]")
            raise typer.Exit(1)

    return app


__all__ = ["build_memory_app"]
