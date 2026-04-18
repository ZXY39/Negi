"""Memory CLI shell bindings for the converged architecture."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.table import Table

import g3ku.agent.memory_agent_runtime as memory_agent_runtime
import g3ku.agent.rag_memory as rag_memory
import g3ku.config.loader as config_loader
from g3ku.resources.tool_settings import MemoryRuntimeSettings, load_tool_settings_from_manifest


def build_memory_app(console) -> typer.Typer:
    app = typer.Typer(help="Manage memory")
    decay_app = typer.Typer(help="Retention and decay operations")
    pending_app = typer.Typer(help="Manage pending memory facts")
    app.add_typer(decay_app, name="decay")
    app.add_typer(pending_app, name="pending")

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

    def _load_legacy_manager():
        config, mem_cfg = _load_settings()
        return config, mem_cfg, rag_memory.MemoryManager(config.workspace_path, mem_cfg)

    def _disabled() -> None:
        console.print('[yellow]Memory is disabled in tools/memory_runtime/resource.yaml -> settings.enabled[/yellow]')

    @app.command('stats')
    def memory_stats():
        _config, mem_cfg, manager = _load_legacy_manager()
        if not mem_cfg.enabled:
            _disabled()
            return
        try:
            stats = asyncio.run(manager.stats())
        finally:
            manager.close()

        table = Table(title='Memory Stats')
        table.add_column('Metric', style='cyan')
        table.add_column('Value', style='green')
        for key in (
            'records', 'records_v2', 'pending', 'records_by_type', 'layer_distribution',
            'planner_calls', 'commit_calls', 'rerank_calls', 'token_in', 'token_out',
            'cost_delta_pct', 'dense_enabled', 'sqlite_path', 'qdrant_path',
        ):
            val = stats.get(key)
            if isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False)
            table.add_row(key, str(val))
        console.print(table)

    @app.command('trace')
    def memory_trace(
        session: str = typer.Option(..., '--session', help='Session id (e.g. cli:direct)'),
        limit: int = typer.Option(20, '--limit', '-n', help='Maximum rows'),
    ):
        _config, mem_cfg, manager = _load_legacy_manager()
        if not mem_cfg.enabled:
            _disabled()
            return
        try:
            rows = asyncio.run(manager.get_traces(session_key=session, limit=limit))
        finally:
            manager.close()

        if not rows:
            console.print('No traces found.')
            return

        table = Table(title=f'Memory Trace ({session})')
        table.add_column('Trace ID', style='cyan')
        table.add_column('Timestamp')
        table.add_column('Plan')
        table.add_column('Candidates')
        table.add_column('Injected')
        table.add_column('Tokens')
        for row in rows:
            table.add_row(
                str(row.get('trace_id', '')),
                str(row.get('timestamp', '')),
                str(len(row.get('plan', []) or [])),
                str(len(row.get('candidates', []) or [])),
                str(len(row.get('injected_blocks', []) or [])),
                str(row.get('token_budget_used', '')),
            )
        console.print(table)

    @app.command('explain')
    def memory_explain(
        query: str = typer.Option(..., '--query', help='User query text'),
        session: str = typer.Option(..., '--session', help='Session id (e.g. cli:direct)'),
    ):
        _config, mem_cfg, manager = _load_legacy_manager()
        if not mem_cfg.enabled:
            _disabled()
            return
        channel, chat_id = (session.split(':', 1) + [''])[:2] if ':' in session else ('cli', session)
        try:
            result = asyncio.run(
                manager.explain_query(query=query, session_key=session, channel=channel, chat_id=chat_id)
            )
        finally:
            manager.close()
        console.print_json(json.dumps(result, ensure_ascii=False))

    @app.command('migrate-v2')
    def memory_migrate_v2(dry_run: bool = typer.Option(False, '--dry-run', help='Preview without writing')):
        _config, mem_cfg, manager = _load_legacy_manager()
        if not mem_cfg.enabled:
            _disabled()
            raise typer.Exit(1)
        try:
            report = asyncio.run(manager.migrate_v2(dry_run=dry_run))
        finally:
            manager.close()
        console.print_json(json.dumps(report, ensure_ascii=False))

    @app.command('reset-runtime')
    def memory_reset_runtime(
        reason: str = typer.Option('manual', '--reason', help='Reason recorded in the reset metadata'),
    ):
        _config, mem_cfg, manager = _load_legacy_manager()
        if not mem_cfg.enabled:
            _disabled()
            raise typer.Exit(1)
        try:
            report = manager.reset_runtime(reason=reason)
        finally:
            manager.close()
        console.print_json(json.dumps(report, ensure_ascii=False))

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

    @decay_app.command('run')
    def memory_decay_run(dry_run: bool = typer.Option(False, '--dry-run', help='Preview deletions only')):
        _config, mem_cfg, manager = _load_legacy_manager()
        if not mem_cfg.enabled:
            _disabled()
            raise typer.Exit(1)
        try:
            report = asyncio.run(manager.run_decay(dry_run=dry_run))
        finally:
            manager.close()
        console.print_json(json.dumps(report, ensure_ascii=False))

    @pending_app.command('list')
    def memory_pending_list(limit: int = typer.Option(50, '--limit', '-n', help='Maximum rows')):
        _config, mem_cfg, manager = _load_legacy_manager()
        if not mem_cfg.enabled:
            _disabled()
            return
        try:
            rows = asyncio.run(manager.list_pending(limit=limit))
        finally:
            manager.close()

        if not rows:
            console.print('No pending facts.')
            return

        table = Table(title='Pending Facts')
        table.add_column('ID', style='cyan')
        table.add_column('Confidence')
        table.add_column('Reason')
        table.add_column('Preview')
        for row in rows:
            preview = row.candidate.replace('\n', ' ')[:120]
            table.add_row(row.pending_id, f'{row.confidence:.2f}', row.reason, preview)
        console.print(table)

    @pending_app.command('approve')
    def memory_pending_approve(pending_id: str = typer.Argument(..., help='Pending fact id')):
        _config, mem_cfg, manager = _load_legacy_manager()
        if not mem_cfg.enabled:
            _disabled()
            raise typer.Exit(1)
        try:
            ok = asyncio.run(manager.update_pending(pending_id, 'approved'))
        finally:
            manager.close()
        if ok:
            console.print(f'[green]OK[/green] Approved pending fact {pending_id}')
        else:
            console.print(f'[red]Pending fact {pending_id} not found[/red]')
            raise typer.Exit(1)

    @pending_app.command('reject')
    def memory_pending_reject(pending_id: str = typer.Argument(..., help='Pending fact id')):
        _config, mem_cfg, manager = _load_legacy_manager()
        if not mem_cfg.enabled:
            _disabled()
            raise typer.Exit(1)
        try:
            ok = asyncio.run(manager.update_pending(pending_id, 'rejected'))
        finally:
            manager.close()
        if ok:
            console.print(f'[green]OK[/green] Rejected pending fact {pending_id}')
        else:
            console.print(f'[red]Pending fact {pending_id} not found[/red]')
            raise typer.Exit(1)

    return app


__all__ = ['build_memory_app']
