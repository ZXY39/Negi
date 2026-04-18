"""Memory CLI shell bindings for the converged architecture."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

import g3ku.agent.memory_agent_runtime as memory_agent_runtime
import g3ku.config.loader as config_loader
from g3ku.resources.tool_settings import MemoryRuntimeSettings, load_tool_settings_from_manifest


def build_memory_app(console) -> typer.Typer:
    app = typer.Typer(help="Manage memory")

    def _load_settings():
        config = config_loader.load_config()
        try:
            mem_cfg = load_tool_settings_from_manifest(config.workspace_path, 'memory_runtime', MemoryRuntimeSettings)
        except Exception as exc:
            console.print(f"[red]Invalid memory runtime settings:[/red] {exc}")
            raise typer.Exit(1) from exc
        return config, mem_cfg

    def _load_runtime_manager(*, read_only_init: bool = False):
        config, mem_cfg = _load_settings()
        if read_only_init:
            try:
                manager = memory_agent_runtime.MemoryManager(
                    config.workspace_path,
                    mem_cfg,
                    read_only_init=True,
                )
            except TypeError as exc:
                # Keep doctor/dry-run compatibility with older two-arg stubs used in CLI tests.
                if "read_only_init" not in str(exc):
                    raise
                manager = memory_agent_runtime.MemoryManager(config.workspace_path, mem_cfg)
        else:
            manager = memory_agent_runtime.MemoryManager(config.workspace_path, mem_cfg)
        return config, mem_cfg, manager

    def _disabled() -> None:
        console.print('[yellow]Memory is disabled in tools/memory_runtime/resource.yaml -> settings.enabled[/yellow]')

    def _print_doctor_report(report: dict[str, object]) -> None:
        console.print("Memory doctor report:")
        if bool(report.get("memory_document_valid")):
            console.print("- MEMORY.md: valid")
        else:
            console.print(f"- MEMORY.md: invalid ({report.get('memory_document_error') or 'unknown error'})")

        missing_note_refs = list(report.get("missing_note_refs") or [])
        if missing_note_refs:
            console.print(f"- Missing note refs: {', '.join(str(item) for item in missing_note_refs)}")
        else:
            console.print("- Missing note refs: none")

        orphan_notes = list(report.get("orphan_notes") or [])
        if orphan_notes:
            console.print(f"- Orphan notes: {', '.join(str(item) for item in orphan_notes)}")
        else:
            console.print("- Orphan notes: none")

        queue_parse_errors = list(report.get("queue_parse_errors") or [])
        if queue_parse_errors:
            console.print(f"- Queue parse errors: {len(queue_parse_errors)} issue(s) in queue.jsonl")
            for item in queue_parse_errors[:3]:
                line_no = item.get("line")
                error_text = str(item.get("error") or "unknown parse error").strip()
                console.print(f"  queue.jsonl line {line_no}: {error_text}")
            if len(queue_parse_errors) > 3:
                console.print(f"  ... plus {len(queue_parse_errors) - 3} more parse issue(s)")
        else:
            console.print("- Queue parse errors: none")

        stuck_head = report.get("stuck_processing_head")
        if isinstance(stuck_head, dict):
            request_id = str(stuck_head.get("request_id") or "").strip() or "(unknown)"
            age_seconds = stuck_head.get("age_seconds")
            age_text = f"{age_seconds}s" if age_seconds is not None else "unknown age"
            console.print(f"- Stuck processing head: {request_id} ({age_text})")
        else:
            console.print("- Stuck processing head: none")

    @app.command('current')
    def memory_current():
        _config, mem_cfg, manager = _load_runtime_manager()
        if not mem_cfg.enabled:
            _disabled()
            return
        try:
            console.print(manager.snapshot_text())
        finally:
            manager.close()

    @app.command('queue')
    def memory_queue(limit: int = typer.Option(20, '--limit', '-n', help='Maximum rows')):
        _config, mem_cfg, manager = _load_runtime_manager()
        if not mem_cfg.enabled:
            _disabled()
            return
        try:
            rows = asyncio.run(manager.list_queue(limit=limit))
        finally:
            manager.close()
        console.print_json(json.dumps(rows, ensure_ascii=False))

    @app.command('flush')
    def memory_flush_once():
        _config, mem_cfg, manager = _load_runtime_manager()
        if not mem_cfg.enabled:
            _disabled()
            raise typer.Exit(1)
        try:
            report = asyncio.run(manager.run_due_batch_once())
        finally:
            manager.close()
        console.print_json(json.dumps(report, ensure_ascii=False))

    @app.command('doctor')
    def memory_doctor(
        now_iso: str = typer.Option('', '--now-iso', help='Override the current timestamp for deterministic checks'),
        stuck_after_seconds: int = typer.Option(
            300,
            '--stuck-after-seconds',
            min=0,
            help='Report a processing queue head as stuck after this many seconds',
        ),
    ):
        _config, mem_cfg, manager = _load_runtime_manager(read_only_init=True)
        if not mem_cfg.enabled:
            _disabled()
            raise typer.Exit(1)
        try:
            report = manager.doctor_report(
                now_iso=(now_iso or "").strip() or None,
                stuck_after_seconds=stuck_after_seconds,
            )
        finally:
            manager.close()
        _print_doctor_report(report)
        if not bool(report.get('ok')):
            raise typer.Exit(1)

    @app.command('reconcile-notes')
    def memory_reconcile_notes(
        delete_orphans: bool = typer.Option(
            False,
            '--delete-orphans',
            help='Delete note files that are no longer referenced by MEMORY.md',
        ),
    ):
        _config, mem_cfg, manager = _load_runtime_manager()
        if not mem_cfg.enabled:
            _disabled()
            raise typer.Exit(1)
        try:
            report = manager.reconcile_notes(delete_orphans=delete_orphans)
        finally:
            manager.close()
        console.print(
            f"Reconciled notes: created {int(report.get('created_missing_count', 0) or 0)} missing notes; "
            f"deleted {int(report.get('deleted_orphan_count', 0) or 0)} orphan notes."
        )
        if not delete_orphans and list(report.get('orphan_notes_detected') or []):
            console.print("Orphan notes were only reported. Re-run with --delete-orphans to remove them.")

    @app.command('import-legacy')
    def memory_import_legacy(
        legacy_path: Path = typer.Argument(..., help='Path to a JSONL or JSON legacy export'),
        apply: bool = typer.Option(
            False,
            '--apply',
            help='Write MEMORY.md and note files. Without this flag the command is dry-run only.',
        ),
    ):
        _config, mem_cfg, manager = _load_runtime_manager(read_only_init=not apply)
        if not mem_cfg.enabled:
            _disabled()
            raise typer.Exit(1)
        try:
            report = manager.import_legacy(legacy_path, apply=apply)
        except Exception as exc:
            console.print(f"[red]import-legacy failed:[/red] {exc}")
            raise typer.Exit(1) from exc
        finally:
            manager.close()
        if apply:
            console.print(
                f"Applied legacy import from {report['legacy_path']}: "
                f"{report['entry_count']} entries, {report['note_ref_count']} note refs."
            )
        else:
            console.print(
                f"Dry-run legacy import from {report['legacy_path']}: "
                f"would import {report['entry_count']} entries and {report['note_ref_count']} note refs."
            )

    @app.command('cleanup-legacy')
    def memory_cleanup_legacy(
        apply: bool = typer.Option(
            False,
            '--apply',
            help='Delete legacy memory artifacts. Without this flag the command is dry-run only.',
        ),
    ):
        _config, mem_cfg, manager = _load_runtime_manager(read_only_init=not apply)
        if not mem_cfg.enabled:
            _disabled()
            raise typer.Exit(1)
        try:
            report = manager.legacy_cleanup_report(apply=apply)
        except Exception as exc:
            console.print(f"[red]cleanup-legacy failed:[/red] {exc}")
            raise typer.Exit(1) from exc
        finally:
            manager.close()

        existing = list(report.get("existing_paths") or [])
        if apply:
            console.print(
                f"Applied legacy cleanup: deleted {len(list(report.get('deleted_paths') or []))} path(s)."
            )
        else:
            console.print(
                f"Dry-run legacy cleanup: found {len(existing)} legacy path(s) to review before deletion."
            )
        if existing:
            console.print("Legacy paths:")
            for item in existing:
                console.print(f"- {item}")
        else:
            console.print("Legacy paths: none")

    return app


__all__ = ['build_memory_app']
