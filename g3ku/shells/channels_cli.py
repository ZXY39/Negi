"""Channel CLI shell bindings for the converged architecture."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.table import Table

import g3ku.config.loader as config_loader


def build_channels_app(console, logo_text: str) -> typer.Typer:
    app = typer.Typer(help="Manage channels")

    @app.command("status")
    def channels_status():
        """Show channel status."""
        config = config_loader.load_config()

        table = Table(title="Channel Status")
        table.add_column("Channel", style="cyan")
        table.add_column("Enabled", style="green")
        table.add_column("Configuration", style="yellow")

        wa = config.channels.whatsapp
        table.add_row("WhatsApp", "enabled" if wa.enabled else "disabled", wa.bridge_url)

        dc = config.channels.discord
        table.add_row("Discord", "enabled" if dc.enabled else "disabled", dc.gateway_url)

        fs = config.channels.feishu
        fs_config = f"app_id: {fs.app_id[:10]}..." if fs.app_id else "[dim]not configured[/dim]"
        table.add_row("Feishu", "enabled" if fs.enabled else "disabled", fs_config)

        mc = config.channels.mochat
        table.add_row("Mochat", "enabled" if mc.enabled else "disabled", mc.base_url or "[dim]not configured[/dim]")

        tg = config.channels.telegram
        tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
        table.add_row("Telegram", "enabled" if tg.enabled else "disabled", tg_config)

        slack = config.channels.slack
        slack_config = "socket" if slack.app_token and slack.bot_token else "[dim]not configured[/dim]"
        table.add_row("Slack", "enabled" if slack.enabled else "disabled", slack_config)

        dt = config.channels.dingtalk
        dt_config = f"client_id: {dt.client_id[:10]}..." if dt.client_id else "[dim]not configured[/dim]"
        table.add_row("DingTalk", "enabled" if dt.enabled else "disabled", dt_config)

        qq = config.channels.qq
        qq_config = f"app_id: {qq.app_id[:10]}..." if qq.app_id else "[dim]not configured[/dim]"
        table.add_row("QQ", "enabled" if qq.enabled else "disabled", qq_config)

        em = config.channels.email
        table.add_row("Email", "enabled" if em.enabled else "disabled", em.imap_host or "[dim]not configured[/dim]")

        console.print(table)

    def _get_bridge_dir() -> Path:
        import shutil
        import subprocess

        from g3ku.utils.helpers import get_data_path

        user_bridge = get_data_path() / "bridge"
        if (user_bridge / "dist" / "index.js").exists():
            return user_bridge

        if not shutil.which("npm"):
            console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
            raise typer.Exit(1)

        pkg_bridge = Path(__file__).parent.parent / "bridge"
        src_bridge = Path(__file__).parent.parent.parent / "bridge"

        source = None
        if (pkg_bridge / "package.json").exists():
            source = pkg_bridge
        elif (src_bridge / "package.json").exists():
            source = src_bridge

        if not source:
            console.print("[red]Bridge source not found.[/red]")
            console.print("Try reinstalling: pip install --force-reinstall g3ku")
            raise typer.Exit(1)

        console.print(f"{logo_text} Setting up bridge...")
        user_bridge.parent.mkdir(parents=True, exist_ok=True)
        if user_bridge.exists():
            shutil.rmtree(user_bridge)
        shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

        try:
            console.print("  Installing dependencies...")
            subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)
            console.print("  Building...")
            subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)
            console.print("[green]OK[/green] Bridge ready\n")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Build failed: {e}[/red]")
            if e.stderr:
                console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
            raise typer.Exit(1)

        return user_bridge

    @app.command("login")
    def channels_login():
        """Link device via QR code."""
        import subprocess

        config = config_loader.load_config()
        bridge_dir = _get_bridge_dir()

        console.print(f"{logo_text} Starting bridge...")
        console.print("Scan the QR code to connect.\n")

        env = {**os.environ}
        if config.channels.whatsapp.bridge_token:
            env["BRIDGE_TOKEN"] = config.channels.whatsapp.bridge_token

        try:
            subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Bridge failed: {e}[/red]")
        except FileNotFoundError:
            console.print("[red]npm not found. Please install Node.js.[/red]")

    return app


__all__ = ["build_channels_app"]
