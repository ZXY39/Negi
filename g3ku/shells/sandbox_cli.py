"""沙箱运维 CLI（`negi sandbox ...`）。

只读优先：`status` 打印安装态、后端与逐维度强制力；`install` / `uninstall` 先打印计划，
必须显式 `--yes` 才执行（与网页的"先确认再动手"同一条纪律）。`persona` 读写群聊人设。
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from g3ku.sandbox.installer import (
    build_install_plan,
    install_status,
    start_install,
    start_uninstall,
)
from g3ku.sandbox.state import (
    install_state,
    refresh_install_state,
    sandbox_persona_text,
    set_sandbox_persona_text,
)


def build_sandbox_app(console) -> typer.Typer:
    app = typer.Typer(help="Sandbox runtime (install state, tiers, persona)")

    def _print_reading(reading: dict) -> None:
        state = str(reading.get("state") or "not_installed")
        color = "green" if state == "ready" else "yellow" if state in {"partial", "runtime_missing"} else "red"
        console.print(f"Install state: [{color}]{state}[/{color}]")
        console.print(f"Backend: {reading.get('backend') or '-'}")
        if reading.get("reason"):
            console.print(f"Reason: {reading['reason']}")
        enforcement = dict(reading.get("enforcement") or {})
        if enforcement:
            parts = [
                f"{name}={enforcement.get(name, 'missing')}"
                for name in ("fs_read", "fs_write", "network", "process", "resource")
            ]
            console.print("Enforcement: " + ", ".join(parts))
        detail = dict(reading.get("detail") or {})
        distros = list(detail.get("distros") or [])
        if distros:
            selected = detail.get("selected_distro") or "-"
            console.print(f"WSL distros: {', '.join(distros)} (selected: {selected})")
        if detail.get("install_hint"):
            console.print(f"Hint: {detail['install_hint']}")

    @app.command("status")
    def status(refresh: bool = typer.Option(False, "--refresh", help="Re-probe instead of using the cached reading")) -> None:
        """安装态、后端与逐维度强制力。"""
        reading = refresh_install_state() if refresh else install_state()
        _print_reading(reading)
        plan = build_install_plan(reading)
        console.print(f"Installable: {'yes' if plan.installable else 'no'}" + (f" ({plan.reason})" if plan.reason else ""))
        run = install_status()
        if run.get("running"):
            console.print(f"Install running: {run.get('phase')}")

    @app.command("install")
    def install(yes: bool = typer.Option(False, "--yes", "-y", help="Execute without prompting")) -> None:
        """按计划安装（先打印步骤与提权要求）。"""
        plan = build_install_plan()
        console.print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        if not plan.installable:
            console.print("[yellow]Nothing to install on this machine.[/yellow]")
            raise typer.Exit(1)
        if not yes and not typer.confirm("Execute these steps?", default=False):
            raise typer.Exit(1)
        result = start_install(confirm=True)
        if not result.get("ok"):
            console.print(f"[red]Install not started:[/red] {result.get('detail')}")
            raise typer.Exit(1)
        console.print("Install started in the background; run `negi sandbox status --refresh` to verify.")

    @app.command("uninstall")
    def uninstall(yes: bool = typer.Option(False, "--yes", "-y", help="Execute without prompting")) -> None:
        """只注销本项目导入的专有发行版；系统包与用户发行版不动。"""
        if not yes and not typer.confirm("Uninstall the sandbox runtime we installed?", default=False):
            raise typer.Exit(1)
        result = start_uninstall(confirm=True)
        if result.get("ok"):
            console.print("Unregistered the dedicated distro. Session volumes are kept.")
            return
        console.print(f"[yellow]{result.get('message') or result.get('detail')}[/yellow]")
        for command in result.get("manual_commands") or []:
            console.print(f"  {command}")
        raise typer.Exit(1)

    @app.command("persona")
    def persona(
        show: bool = typer.Option(False, "--show", help="Print the current persona"),
        clear: bool = typer.Option(False, "--clear", help="Remove the persona (group chats go without one)"),
        set_text: Optional[str] = typer.Option(None, "--set", help="Set the persona text"),
    ) -> None:
        """第三方会话（群聊）用的人设；与操作员自己的人设 note 分开。"""
        if clear:
            set_sandbox_persona_text("")
            console.print("Persona cleared.")
            return
        if set_text is not None:
            set_sandbox_persona_text(set_text)
            console.print("Persona saved.")
            return
        _ = show
        text = sandbox_persona_text()
        console.print(text if text else "[dim](empty — group chats run without a persona)[/dim]")

    return app


__all__ = ["build_sandbox_app"]
