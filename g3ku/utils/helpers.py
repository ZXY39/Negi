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


def expand_path_anchor_tokens(
    raw_path: str | Path,
    *,
    workspace: Path | None,
    temp_root: Path | None = None,
    data_root: Path | None = None,
) -> str:
    """Substitute the anchor tokens a model may use instead of retyping the prefix."""
    text = str(raw_path or "")
    targets = {
        "{workspace}": str(workspace) if workspace is not None else "",
        "{temp}": str(temp_root) if temp_root is not None else "",
        "{data}": str(data_root) if data_root is not None else "",
    }
    for token, target in targets.items():
        if token in text:
            if not target:
                raise ValueError(
                    f"{token} is not available in this runtime; use an absolute path or a path relative to the workspace"
                )
            text = text.replace(token, target)
    return text


def model_data_root(workspace: Path | None) -> Path | None:
    """Return the data root only when it really diverges from the project root.

    While both resolve to the same directory, a second name for it gives the model two
    tokens with one meaning, so it is withheld until it carries distinct information.
    """
    try:
        from g3ku.deployment.data_root import data_root

        root = Path(data_root())
    except Exception:
        return None
    if workspace is None:
        return root
    try:
        return None if root.resolve() == Path(workspace).resolve() else root
    except Exception:
        return None


def resolve_model_path(
    raw_path: str | Path,
    *,
    workspace: Path | None,
    temp_root: Path | None = None,
    data_root: Path | None = None,
) -> Path:
    """Bind a model-supplied path to its anchor without second-guessing the model.

    A bare filename has no anchor to reason about, so it belongs to the caller's scratch
    area; a relative path with directory segments is read against the project instead.
    Absolute paths are never rewritten.
    """
    text = expand_path_anchor_tokens(
        raw_path,
        workspace=workspace,
        temp_root=temp_root,
        data_root=data_root,
    ).strip()
    if not text:
        raise ValueError("path is required")
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        return candidate
    if temp_root is not None and not re.search(r"[\\/]", text):
        return Path(temp_root) / candidate
    if workspace is None:
        raise ValueError(f"relative path is not allowed; provide absolute path: {raw_path}")
    return Path(workspace) / candidate


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')
_ACTIVE_WORKSPACE_TEMPLATE_FILES: tuple[tuple[str, str], ...] = (
    ("memory/MEMORY.md", "memory/MEMORY.md"),
)
_ACTIVE_WORKSPACE_PLACEHOLDERS: tuple[str, ...] = ()


def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync active bundled templates to workspace. Only creates missing files."""
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
        added.append(dest.relative_to(workspace).as_posix())

    for src_rel, dest_rel in _ACTIVE_WORKSPACE_TEMPLATE_FILES:
        _write(tpl.joinpath(*Path(src_rel).parts), workspace / dest_rel)
    for dest_rel in _ACTIVE_WORKSPACE_PLACEHOLDERS:
        _write(None, workspace / dest_rel)
    (workspace / "skills").mkdir(exist_ok=True)

    if added and not silent:
        from rich.console import Console
        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added

