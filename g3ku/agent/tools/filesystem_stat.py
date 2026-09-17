"""Read-only filesystem measurement tool.

验收节点需要一条能真实测量产物的只读通道：存在性、磁盘真实字节数、mtime，以及有界的
目录清单与体积聚合。没有它，节点只能拿内容工具对二进制文件返回的占位串统计去猜文件状态
（2026-09-17 的「34 字节空壳」误判即由此而来），也无法在不逐个打开文件的前提下回答
「这批产物有几个、多大、是不是本轮改的」。

工具只做 stat / iterdir / 递归聚合，不写、不删、不改——inspection 角色的裁判独立性据此保留。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool

# 目录聚合的上限：防御病态目录树，同时保证正常交付目录（数百文件）一次调用即可测全。
_MAX_SCAN_FILES = 200_000
_MAX_SCAN_DEPTH = 8
_DEFAULT_MAX_ENTRIES = 200
_MAX_ENTRIES_LIMIT = 2000


def _iso_mtime(value: float) -> str:
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


class FilesystemStatTool(Tool):
    def __init__(self, *, workspace: Path | None = None, allowed_dir: Path | None = None) -> None:
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "filesystem_stat"

    @property
    def description(self) -> str:
        return (
            "Read-only measurement of files and directories: existence, real on-disk size in bytes, "
            "mtime, and bounded directory listings with size aggregates."
        )

    @property
    def model_description(self) -> str:
        return (
            "Read-only measurement of files and directories: existence, real on-disk size in bytes, mtime, "
            "and bounded directory listings with aggregates (file count, total/min/max size). "
            "Use this to verify delivered artifacts as a set instead of guessing sizes, and to get real "
            "file names instead of constructing them. Never modifies anything."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string", "description": "Absolute file or directory path."},
                    "description": "Paths to measure. Files return size/mtime; directories return bounded listings and aggregates.",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Max entries listed per directory (default 200, max 2000). Aggregates always cover the whole tree.",
                    "minimum": 1,
                    "maximum": _MAX_ENTRIES_LIMIT,
                },
            },
            "required": ["paths"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        paths = (params or {}).get("paths")
        if not isinstance(paths, list) or not [item for item in paths if str(item or "").strip()]:
            errors.append("paths must contain at least one non-empty path")
        return errors

    async def execute(
        self,
        paths: list[str],
        max_entries: int | None = None,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = kwargs, __g3ku_runtime
        entry_cap = max(1, min(int(max_entries or _DEFAULT_MAX_ENTRIES), _MAX_ENTRIES_LIMIT))
        items = [self._measure_one(str(raw or "").strip(), entry_cap=entry_cap) for raw in list(paths or [])]
        items = [item for item in items if item]
        missing = [item["path"] for item in items if not item.get("exists")]
        file_count = sum(1 for item in items if item.get("kind") == "file")
        dir_count = sum(1 for item in items if item.get("kind") == "directory")
        total_bytes = sum(int(item.get("size_bytes") or 0) for item in items if item.get("kind") == "file")
        total_bytes += sum(int(item.get("total_bytes") or 0) for item in items if item.get("kind") == "directory")
        summary = (
            f"{len(items)} paths measured: {file_count} files ({total_bytes} bytes total), "
            f"{dir_count} directories, {len(missing)} missing."
        )
        return {
            "ok": True,
            "items": items,
            "summary": summary,
            "missing": missing,
            "measured_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _measure_one(self, raw_path: str, *, entry_cap: int) -> dict[str, Any] | None:
        if not raw_path:
            return None
        try:
            target = self._resolve_path(raw_path)
        except PermissionError as exc:
            return {"path": raw_path, "exists": False, "error": str(exc)}
        item: dict[str, Any] = {"path": str(target)}
        try:
            info = target.stat()
        except OSError:
            return {**item, "exists": False}
        item["exists"] = True
        item["mtime"] = _iso_mtime(info.st_mtime)
        if target.is_dir():
            item["kind"] = "directory"
            item.update(self._measure_directory(target, entry_cap=entry_cap))
            return item
        item["kind"] = "file"
        item["size_bytes"] = int(info.st_size)
        item["suffix"] = target.suffix.lower()
        return item

    def _measure_directory(self, directory: Path, *, entry_cap: int) -> dict[str, Any]:
        """递归聚合目录：文件数、总体积、最大/最小文件，外加有界条目清单。"""
        file_count = 0
        dir_count = 0
        total_bytes = 0
        largest: dict[str, Any] | None = None
        smallest: dict[str, Any] | None = None
        truncated = False

        def _visit(current: Path, depth: int) -> None:
            nonlocal file_count, dir_count, total_bytes, largest, smallest, truncated
            if truncated:
                return
            try:
                children = sorted(current.iterdir(), key=lambda entry: entry.name.lower())
            except OSError:
                return
            for child in children:
                try:
                    if child.is_dir():
                        dir_count += 1
                        if depth < _MAX_SCAN_DEPTH:
                            _visit(child, depth + 1)
                        continue
                    child_info = child.stat()
                except OSError:
                    continue
                file_count += 1
                if file_count > _MAX_SCAN_FILES:
                    truncated = True
                    return
                size = int(child_info.st_size)
                total_bytes += size
                candidate = {
                    "name": child.name,
                    "path": str(child),
                    "size_bytes": size,
                    "mtime": _iso_mtime(child_info.st_mtime),
                }
                if largest is None or size > largest["size_bytes"]:
                    largest = candidate
                if smallest is None or size < smallest["size_bytes"]:
                    smallest = candidate

        _visit(directory, 0)
        entries: list[dict[str, Any]] = []
        try:
            for child in sorted(directory.iterdir(), key=lambda entry: entry.name.lower())[:entry_cap]:
                try:
                    child_stat = child.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "kind": "dir" if child.is_dir() else "file",
                        "size_bytes": int(child_stat.st_size) if child.is_file() else None,
                        "mtime": _iso_mtime(child_stat.st_mtime),
                    }
                )
        except OSError:
            pass
        payload: dict[str, Any] = {
            "file_count": file_count,
            "dir_count": dir_count,
            "total_bytes": total_bytes,
            "entries": entries,
            "entries_truncated": file_count > entry_cap,
        }
        if largest is not None:
            payload["largest_file"] = largest
            payload["smallest_file"] = smallest
        if truncated:
            payload["scan_truncated"] = True
        return payload

    def _resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute() and self._workspace is not None:
            path = self._workspace / path
        resolved = path.resolve()
        if self._allowed_dir is not None:
            try:
                resolved.relative_to(self._allowed_dir.resolve())
            except ValueError as exc:
                raise PermissionError(f"path outside allowed directory: {raw_path}") from exc
        return resolved


def build_filesystem_stat_tool(runtime: Any) -> FilesystemStatTool:
    from g3ku.resources.tool_settings import FilesystemStatToolSettings, runtime_tool_settings

    settings = runtime_tool_settings(runtime, FilesystemStatToolSettings, tool_name="filesystem_stat")
    workspace = getattr(runtime, "workspace", None)
    workspace_path = Path(workspace) if workspace else None
    allowed_dir = workspace_path if (workspace_path is not None and settings.restrict_to_workspace) else None
    return FilesystemStatTool(workspace=workspace_path, allowed_dir=allowed_dir)
