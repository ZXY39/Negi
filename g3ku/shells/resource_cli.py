from __future__ import annotations

import typer

from g3ku.config.loader import load_config
from g3ku.resources import ResourceKind, get_shared_resource_manager


def build_resource_app(console) -> typer.Typer:
    app = typer.Typer(help="Inspect and reload root-level skills/tools resources.")

    def _manager():
        config = load_config()
        manager = get_shared_resource_manager(config.workspace_path, app_config=config)
        return manager

    @app.command("list")
    def resource_list() -> None:
        manager = _manager()
        console.print("[bold]Tools[/bold]")
        for descriptor in manager.list_tools():
            state = manager.busy_state(ResourceKind.TOOL, descriptor.name)
            console.print(f"- {descriptor.name} available={descriptor.available} busy={state.busy} pending_delete={state.pending_delete}")
        console.print("[bold]Skills[/bold]")
        for descriptor in manager.list_skills():
            state = manager.busy_state(ResourceKind.SKILL, descriptor.name)
            console.print(f"- {descriptor.name} available={descriptor.available} busy={state.busy} pending_delete={state.pending_delete}")

    @app.command("reload")
    def resource_reload() -> None:
        manager = _manager()
        snapshot = manager.reload_now(trigger="cli")
        console.print(f"Reloaded resources: {len(snapshot.tools)} tools, {len(snapshot.skills)} skills")

    @app.command("validate")
    def resource_validate() -> None:
        manager = _manager()
        snapshot = manager.reload_now(trigger="validate")
        errors: list[str] = []
        warnings: list[str] = []
        for kind, descriptors in (("tool", snapshot.tools), ("skill", snapshot.skills)):
            for descriptor in descriptors.values():
                errors.extend(f"{kind}:{descriptor.name}: {item}" for item in list(descriptor.errors or []))
                warnings.extend(f"{kind}:{descriptor.name}: {item}" for item in list(descriptor.warnings or []))
        for issue in [*errors, *warnings]:
            console.print(f"- {issue}")
        if errors:
            # 校验是给人和 CI 当闸门用的：报错必须以非零退出收尾，否则坏资源树
            # 在脚本里与干净资源树长得一样。警告只提示，不算失败。
            raise typer.Exit(1)
        if not warnings:
            console.print("All resources validated cleanly")

    @app.command("status")
    def resource_status() -> None:
        manager = _manager()
        snapshot = manager.reload_now(trigger="status")
        console.print(f"generation={snapshot.generation}")
        console.print(f"tools={len(snapshot.tools)} skills={len(snapshot.skills)}")

    return app


__all__ = ["build_resource_app"]
