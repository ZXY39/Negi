"""Utility functions for g3ku."""

import re
from datetime import datetime
from pathlib import Path
from typing import Any


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


PATH_ANCHOR_RULE_TEXT = (
    '裸文件名或 `{temp}/<名>` 落到本次调用的临时目录；带目录段的相对路径与 `{workspace}/<相对路径>` 落到项目根；'
    '绝对路径按给定使用，任何一层都不重写。不要逐字重抄整条绝对前缀——它比短写法更容易打错。'
    '`{data}` 只在数据根与项目根分离时可用。'
)


def path_anchor_tokens(*, include_data: bool = False) -> str:
    """The token set advertised to the model; the resolver is the real authority."""
    tokens = ['{workspace}', '{temp}']
    if include_data:
        tokens.append('{data}')
    return ' | '.join(tokens)


def path_anchor_policy(workspace: Path | None) -> dict[str, Any]:
    """Single source for the path anchoring contract, shared by both lanes."""
    return {
        'relative_paths_bind_to_workspace': True,
        'bare_filename_binds_to_task_temp_dir': True,
        'path_anchor_tokens': path_anchor_tokens(
            include_data=workspace is not None and model_data_root(workspace) is not None
        ),
        'path_anchor_rule': PATH_ANCHOR_RULE_TEXT,
    }


UNDECLARED_CANDIDATE_LINE_PREFIX = (
    'undeclared_candidates (`能 `load_tool_context` 但参数表还没进本次 `tools[]`，'
    '清单到压缩那一跳才重印)'
)


def render_candidate_tool_line(
    candidate_names: Any,
    declared_names: Any,
) -> list[str]:
    """候选行只有一种形状，两车道共用同一个函数渲染，保证同形是构造出来的而不是抄的。

    - 拿到声明名单（本次真正带出去的 `tools[]`）时，只渲差集"候选 − 已声明"，为空就整行不出现。
      全量候选表删掉的理由：钉住清单带着全角色的参数表，`callable_tools` 与 `denied_tools`
      同块在场，"哪些还能 load"＝`tools[] − callable − denied − 控制工具`，模型在同一块里读得出来。
      留下的差集行是**声明滞后的出口**：`tools[]` 到压缩那一跳才重印，刚被治理放行、对象字典还没
      建出它的名字会先能 `load_tool_context` 后上清单（节点道候选池上界是每跳活的治理读）。
    - 拿不到声明名单时退回渲全量并沿用 `candidate_tools` 标签：省略的前提是"可推导"成立，
      证明不了就宁可重复不可缺失。
    """
    declared = [
        str(item or '').strip()
        for item in list(declared_names or [])
        if str(item or '').strip()
    ]
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(candidate_names or []):
        name = str(item or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    if not ordered:
        return []
    rendered = ', '.join(f'`{name}`' for name in ordered)
    if not declared:
        return [f'candidate_tools: {rendered}']
    remaining = [name for name in ordered if name not in set(declared)]
    if not remaining:
        return []
    return [
        f'{UNDECLARED_CANDIDATE_LINE_PREFIX}: '
        + ', '.join(f'`{name}`' for name in remaining)
    ]


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
        raise ValueError(f"no project root to bind a relative path to; provide an absolute path: {raw_path}")
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

