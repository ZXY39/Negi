from __future__ import annotations

import typer


def build_provider_app(console=None, logo_text: str = '') -> typer.Typer:
    app = typer.Typer(help='Provider account and login utilities.')

    @app.command('status')
    def provider_status() -> None:
        _ = logo_text
        if console is not None:
            console.print('[yellow]Provider CLI is not implemented in this workspace build.[/yellow]')
        else:
            print('Provider CLI is not implemented in this workspace build.')

    return app


__all__ = ['build_provider_app']

