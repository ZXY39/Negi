"""Utility functions for g3ku."""

import re
from datetime import datetime
from pathlib import Path


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_path() -> Path:
    """Workspace-scoped data directory (./.g3ku)."""
    return ensure_dir(Path.cwd() / ".g3ku")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure workspace path. Defaults to current directory."""
    path = Path(workspace).expanduser() if workspace else Path.cwd()
    return ensure_dir(path)


def resolve_path_in_workspace(raw_path: str | Path, workspace: Path) -> Path:
    """Resolve and force a path under workspace.

    Supports:
    - `~` expansion
    - `{workspace}` token substitution
    - relative paths rooted at `workspace`
    - absolute paths re-based into `workspace`
    """
    workspace_root = Path(workspace).expanduser().resolve()
    text = str(raw_path)
    if "{workspace}" in text:
        text = text.replace("{workspace}", str(workspace_root))
    resolved = Path(text).expanduser()
    if not resolved.is_absolute():
        return (workspace_root / resolved).resolve()

    # Keep absolute paths already under workspace.
    try:
        resolved.relative_to(workspace_root)
        return resolved.resolve()
    except Exception:
        pass

    # Re-base external absolute paths into workspace.
    parts = list(resolved.parts)
    if parts and parts[0] == resolved.anchor:
        parts = parts[1:]
    forced = workspace_root.joinpath(*parts) if parts else workspace_root
    return forced.resolve()


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')

def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files."""
    from importlib.resources import files as pkg_files
    try:
        tpl = pkg_files("g3ku") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    for item in tpl.iterdir():
        if item.name.endswith(".md"):
            _write(item, workspace / item.name)
    _write(tpl / "memory" / "MEMORY.md", workspace / "memory" / "MEMORY.md")
    _write(None, workspace / "memory" / "HISTORY.md")
    (workspace / "skills").mkdir(exist_ok=True)

    if added and not silent:
        from rich.console import Console
        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added

