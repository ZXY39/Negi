"""Security CLI: inspect, snapshot and restore the project's own key material.

The credential store is a few small files that are not in git and not covered by any system
backup, and losing them makes a configured project report itself as a brand-new one. These
commands are the operator's way to see that state, take a copy deliberately, and roll back to
one of the automatic generations.
"""

from __future__ import annotations

from pathlib import Path

import typer

import g3ku.config.loader as config_loader
from g3ku.security.key_store_backup import (
    audit_key_store_continuity,
    configured_key_material,
    list_backups,
    read_events,
    restore_from_backup,
    snapshot_key_store,
)


def build_security_app(console) -> typer.Typer:
    app = typer.Typer(help="Inspect and restore the key store")

    def _workspace() -> Path:
        return Path(config_loader.load_config().workspace_path).resolve()

    @app.command("status")
    def status_command() -> None:
        """Show whether key material is present and which backups exist."""
        workspace = _workspace()
        material = configured_key_material(workspace)
        finding = audit_key_store_continuity(workspace, reason="cli_status")
        console.print(f"workspace: {workspace}")
        console.print(f"state: {finding.get('state')}")
        console.print(f"present: {', '.join(material['present']) or '-'}")
        if material["empty"]:
            console.print(f"[red]zero-length (damaged): {', '.join(material['empty'])}[/red]")
        console.print(f"missing: {', '.join(material['missing']) or '-'}")
        generations = list_backups(workspace)
        console.print(f"backups: {len(generations)}")
        for path in generations[:5]:
            console.print(f"  {path.name}")
        for event in read_events(workspace, limit=5):
            console.print(f"  event {event.get('at')} {event.get('event')} {event.get('reason') or ''}")

    @app.command("backup")
    def backup_command() -> None:
        """Take one snapshot of the current key material."""
        workspace = _workspace()
        generation = snapshot_key_store(workspace, reason="cli_backup", force=True)
        if generation is None:
            console.print("[yellow]nothing to back up (no master key, record file or realm overlay)[/yellow]")
            raise typer.Exit(1)
        console.print(f"backed up to {generation}")

    @app.command("restore")
    def restore_command(
        generation: str = typer.Option("", "--from", help="Backup generation name; defaults to the newest"),
        apply: bool = typer.Option(False, "--apply", help="Actually write files (default is a dry run)"),
    ) -> None:
        """Restore key material from a backup generation."""
        workspace = _workspace()
        chosen = generation.strip()
        if not chosen:
            generations = list_backups(workspace)
            if not generations:
                console.print("[red]no backup generations under .g3ku/llm-config-backups[/red]")
                raise typer.Exit(1)
            chosen = generations[0].name
        try:
            report = restore_from_backup(workspace, chosen, dry_run=not apply)
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        verb = "restored" if report["dry_run"] is False else "would restore"
        console.print(f"{verb} {len(report['files'])} file(s) from {report['source']}")
        for name in report["files"]:
            console.print(f"  {name}")
        if report["dry_run"]:
            console.print("[dim]re-run with --apply to write these files[/dim]")

    return app
