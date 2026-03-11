"""Capability CLI shell bindings for the converged architecture."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from g3ku.capabilities.installer import CapabilityInstaller
from g3ku.capabilities.registry import CapabilityRegistry
from g3ku.capabilities.source_registry import CapabilitySourcePolicy, CapabilitySourceRegistry
from g3ku.capabilities.validator import CapabilityValidator
import g3ku.config.loader as config_loader
from g3ku.utils.helpers import resolve_path_in_workspace


def build_capability_app(console) -> typer.Typer:
    app = typer.Typer(help="Manage capability packs")

    def _load_capability_stack():
        config = config_loader.load_config()
        cap_cfg = config.tools.capabilities
        registry = CapabilityRegistry(
            config.workspace_path,
            workspace_dir=resolve_path_in_workspace(cap_cfg.workspace_dir, config.workspace_path),
            state_path=resolve_path_in_workspace(cap_cfg.state_path, config.workspace_path),
            admin_enabled=bool(cap_cfg.admin_enabled),
        )
        source_registry = CapabilitySourceRegistry(
            CapabilitySourcePolicy(
                allow_local=bool(cap_cfg.allow_local),
                allow_git=bool(cap_cfg.allow_git),
                allowed_git_hosts=[str(item) for item in (cap_cfg.allowed_git_hosts or [])],
            )
        )
        index_paths = []
        for raw_path in (cap_cfg.index_paths or []):
            candidate = Path(raw_path).expanduser()
            index_paths.append(candidate if candidate.is_absolute() else resolve_path_in_workspace(candidate, config.workspace_path))
        from g3ku.capabilities.index_registry import CapabilityIndexRegistry

        index_registry = CapabilityIndexRegistry(config.workspace_path, index_paths=index_paths)
        installer = CapabilityInstaller(registry, source_registry=source_registry, index_registry=index_registry)
        validator = CapabilityValidator(registry)
        return config, registry, installer, validator

    def _print_capability_result(result: Any) -> None:
        prefix = "[dry-run] " if getattr(result, "dry_run", False) else ""
        console.print(prefix + result.message)
        for warning in getattr(result, "warnings", []) or []:
            console.print(f"[yellow]warning:[/] {warning}")
        for error in getattr(result, "errors", []) or []:
            console.print(f"[red]error:[/] {error}")

    @app.command("search")
    def capability_search(query: str = typer.Argument("", help="Optional package name filter.")):
        """Search capability packages available in configured indexes."""
        _, _, installer, _ = _load_capability_stack()
        rows = installer.search(query)
        if not rows:
            console.print("No indexed capabilities found.")
            return
        table = Table(title="Indexed Capability Packages")
        table.add_column("Name", style="cyan")
        table.add_column("Version")
        table.add_column("Source")
        table.add_column("Index")
        for row in rows:
            table.add_row(
                row.name,
                row.version,
                row.source.type,
                str(row.index_name or row.index_path or ""),
            )
        console.print(table)

    @app.command("sources")
    def capability_sources():
        """Show allowed capability source types and current source policy."""
        _, _, installer, _ = _load_capability_stack()
        rows = installer.list_sources()
        table = Table(title="Capability Sources")
        table.add_column("Type", style="cyan")
        table.add_column("Enabled")
        table.add_column("Notes")
        for row in rows:
            table.add_row(
                str(row.get("type") or ""),
                "yes" if row.get("enabled") else "no",
                " | ".join(str(item) for item in (row.get("notes") or [])),
            )
        console.print(table)

    @app.command("list")
    def capability_list():
        """List builtin and workspace capability packs."""
        _, _, installer, _ = _load_capability_stack()
        rows = installer.list()
        if not rows:
            console.print("No capabilities found.")
            return
        table = Table(title="Capability Packs")
        table.add_column("Name", style="cyan")
        table.add_column("Version")
        table.add_column("Enabled")
        table.add_column("Available")
        table.add_column("Source")
        table.add_column("Installed Path")
        for row in rows:
            table.add_row(
                row["name"],
                str(row.get("version") or ""),
                "yes" if row.get("enabled") else "no",
                "yes" if row.get("available") else "no",
                str(row.get("source") or ""),
                str(row.get("installed_path") or row.get("uri") or ""),
            )
        console.print(table)

    @app.command("validate")
    def capability_validate(name: str | None = typer.Argument(None, help="Optional capability name to validate.")):
        """Validate one capability pack or all capability packs."""
        _, _, _, validator = _load_capability_stack()
        results = [validator.validate_capability(name)] if name else validator.validate_all()
        failed = [item for item in results if not item.ok]
        for result in results:
            status = "OK" if result.ok else "FAIL"
            console.print(f"[{ 'green' if result.ok else 'red' }]{status}[/] {result.name}")
            for warning in result.warnings:
                console.print(f"  [yellow]warning:[/] {warning}")
            for error in result.errors:
                console.print(f"  [red]error:[/] {error}")
        if failed:
            raise typer.Exit(1)

    @app.command("enable")
    def capability_enable(
        name: str = typer.Argument(..., help="Capability name."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview without applying changes."),
    ):
        """Enable a capability pack."""
        _, _, installer, _ = _load_capability_stack()
        result = installer.enable(name, dry_run=dry_run)
        _print_capability_result(result)
        if not result.ok:
            raise typer.Exit(1)

    @app.command("disable")
    def capability_disable(
        name: str = typer.Argument(..., help="Capability name."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview without applying changes."),
    ):
        """Disable a capability pack."""
        _, _, installer, _ = _load_capability_stack()
        result = installer.disable(name, dry_run=dry_run)
        _print_capability_result(result)
        if not result.ok:
            raise typer.Exit(1)

    @app.command("init")
    def capability_init(
        name: str = typer.Argument(..., help="Capability name."),
        capability_type: str = typer.Option("hybrid", "--type", help="Capability type: tool, skill, or hybrid."),
    ):
        """Scaffold a new workspace capability pack."""
        _, _, installer, _ = _load_capability_stack()
        result = installer.init_capability(name=name, capability_type=capability_type)
        console.print(result.message)
        if not result.ok:
            raise typer.Exit(1)

    @app.command("install")
    def capability_install(
        source: str = typer.Argument(..., help="Local capability path, git repository URL, or registry package name."),
        source_type: str = typer.Option("local", "--source-type", help="Install from local path, git, or registry."),
        ref: str | None = typer.Option(None, "--ref", help="Optional git ref."),
        version: str | None = typer.Option(None, "--version", help="Optional registry version to install."),
        index_name: str | None = typer.Option(None, "--index", help="Optional registry index name filter."),
        enable: bool = typer.Option(True, "--enable/--disable", help="Enable after install."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview without applying changes."),
    ):
        """Install a capability pack from a local path, git repository, or configured registry index."""
        _, _, installer, _ = _load_capability_stack()
        if source_type == "git":
            result = installer.install_from_git(source, ref=ref, enable=enable, dry_run=dry_run)
        elif source_type == "registry":
            result = installer.install_from_registry(source, version=version, index_name=index_name, enable=enable, dry_run=dry_run)
        else:
            result = installer.install_from_path(source, enable=enable, dry_run=dry_run)
        _print_capability_result(result)
        if not result.ok:
            raise typer.Exit(1)

    @app.command("update")
    def capability_update(
        name: str = typer.Argument(..., help="Capability name."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview without applying changes."),
    ):
        """Update a capability pack from its recorded source."""
        _, _, installer, _ = _load_capability_stack()
        result = installer.update(name, dry_run=dry_run)
        _print_capability_result(result)
        if not result.ok:
            raise typer.Exit(1)

    @app.command("remove")
    def capability_remove(
        name: str = typer.Argument(..., help="Capability name."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview without applying changes."),
    ):
        """Remove a workspace capability pack or clear builtin override state."""
        _, _, installer, _ = _load_capability_stack()
        result = installer.remove(name, dry_run=dry_run)
        _print_capability_result(result)
        if not result.ok:
            raise typer.Exit(1)

    return app


__all__ = ["build_capability_app"]

