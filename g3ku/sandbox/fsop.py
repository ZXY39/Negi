"""jail 内的一次性文件助手。

`filesystem_*` / `content_*` 是**进程内 Python I/O**，包一层 argv 管不到它们。所以文件
操作改由 jail 里的一次性子进程执行：``python -m g3ku.sandbox.fsop`` 读一份结构化请求、
按根白名单解析路径、做完就退出。内核边界由 jail 提供，这里再做一层"路径必须落在允许根内"
的语义校验（防 profile 写错、防 symlink 指向根外）。

**不拼 shell、不接收裸命令行**：请求走 stdin 的 JSON，路径与内容都当数据。
输出永远是单行 JSON（``ok`` / ``kind`` / 载荷），调用方按结构读，不解析人话。

依赖只有标准库：jail 里只需要一个 python3。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

KIND_DENIED = "denied"
KIND_INVALID = "invalid"
KIND_IO = "io"

DEFAULT_MAX_READ_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_LIST_ENTRIES = 500
DEFAULT_MAX_SEARCH_MATCHES = 50
_MAX_LIST_ENTRIES_LIMIT = 5000
_MAX_SEARCH_LIMIT = 500


class FsOpError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _roots_from_argv(argv: list[str]) -> tuple[Path, ...]:
    roots: list[Path] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--root" and index + 1 < len(argv):
            candidate = Path(argv[index + 1]).expanduser()
            roots.append(candidate.resolve())
            index += 2
            continue
        index += 1
    if not roots:
        raise FsOpError(KIND_INVALID, "--root is required")
    return tuple(roots)


def _resolve(raw: Any, roots: tuple[Path, ...]) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise FsOpError(KIND_INVALID, "path is required")
    candidate = Path(text).expanduser()
    resolved = candidate.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise FsOpError(KIND_DENIED, f"path is outside the allowed roots: {text}")


def _read(request: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, Any]:
    path = _resolve(request.get("path"), roots)
    if not path.is_file():
        raise FsOpError(KIND_INVALID, f"not a file: {request.get('path')}")
    limit = int(request.get("max_bytes") or DEFAULT_MAX_READ_BYTES)
    limit = max(1, min(limit, DEFAULT_MAX_READ_BYTES))
    data = path.read_bytes()
    truncated = len(data) > limit
    text = data[:limit].decode("utf-8", errors="replace")
    start_line = max(1, int(request.get("start_line") or 1))
    end_line = request.get("end_line")
    lines = text.splitlines()
    if end_line is not None:
        end_line = max(start_line, int(end_line))
        lines = lines[start_line - 1 : end_line]
    elif start_line > 1:
        lines = lines[start_line - 1 :]
    body = "\n".join(lines)
    return {
        "path": str(path),
        "text": body,
        "line_count": len(text.splitlines()),
        "truncated": truncated,
    }


def _write(request: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, Any]:
    path = _resolve(request.get("path"), roots)
    content = request.get("content")
    if not isinstance(content, str):
        raise FsOpError(KIND_INVALID, "content must be a string")
    if bool(request.get("create_dirs", True)):
        path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path),
        "bytes": len(content.encode("utf-8")),
        "created": not existed,
    }


def _replace(request: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, Any]:
    path = _resolve(request.get("path"), roots)
    old = request.get("old")
    new = request.get("new")
    if not isinstance(old, str) or not old:
        raise FsOpError(KIND_INVALID, "old must be a non-empty string")
    if not isinstance(new, str):
        raise FsOpError(KIND_INVALID, "new must be a string")
    if not path.is_file():
        raise FsOpError(KIND_INVALID, f"not a file: {request.get('path')}")
    text = path.read_text(encoding="utf-8")
    count = int(request.get("count") or 1)
    occurrences = text.count(old)
    if occurrences == 0:
        raise FsOpError(KIND_INVALID, "old text not found")
    if count <= 0 or count > occurrences:
        count = occurrences
    updated = text.replace(old, new, count)
    path.write_text(updated, encoding="utf-8")
    return {"path": str(path), "replacements": count, "occurrences": occurrences}


def _list(request: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, Any]:
    path = _resolve(request.get("path"), roots)
    if not path.exists():
        raise FsOpError(KIND_INVALID, f"no such path: {request.get('path')}")
    if path.is_file():
        return {"path": str(path), "kind": "file", "size": path.stat().st_size, "entries": []}
    limit = int(request.get("max_entries") or DEFAULT_MAX_LIST_ENTRIES)
    limit = max(1, min(limit, _MAX_LIST_ENTRIES_LIMIT))
    entries: list[dict[str, Any]] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        try:
            info = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "kind": "dir" if child.is_dir() else "file",
                "size": info.st_size,
            }
        )
        if len(entries) >= limit:
            break
    return {"path": str(path), "kind": "dir", "entries": entries, "truncated": len(entries) >= limit}


def _stat(request: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, Any]:
    raw_paths = request.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise FsOpError(KIND_INVALID, "paths must be a non-empty list")
    items: list[dict[str, Any]] = []
    for raw in raw_paths:
        path = _resolve(raw, roots)
        try:
            info = path.stat()
        except OSError:
            items.append({"path": str(path), "exists": False})
            continue
        items.append(
            {
                "path": str(path),
                "exists": True,
                "kind": "dir" if path.is_dir() else "file",
                "size": info.st_size,
                "mtime": info.st_mtime,
            }
        )
    return {"items": items}


def _search(request: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, Any]:
    path = _resolve(request.get("path"), roots)
    query = str(request.get("query") or "")
    if not query:
        raise FsOpError(KIND_INVALID, "query is required")
    if not path.is_file():
        raise FsOpError(KIND_INVALID, f"not a file: {request.get('path')}")
    limit = int(request.get("limit") or DEFAULT_MAX_SEARCH_MATCHES)
    limit = max(1, min(limit, _MAX_SEARCH_LIMIT))
    use_regex = bool(request.get("regex", False))
    pattern = None
    if use_regex:
        try:
            pattern = re.compile(query)
        except re.error as exc:
            raise FsOpError(KIND_INVALID, f"invalid regex: {exc}") from exc
    matches: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        hit = bool(pattern.search(line)) if pattern is not None else query in line
        if not hit:
            continue
        matches.append({"line": number, "text": line[:400]})
        if len(matches) >= limit:
            break
    return {"path": str(path), "matches": matches, "truncated": len(matches) >= limit}


def _edit(request: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, Any]:
    path = _resolve(request.get("path"), roots)
    if not path.is_file():
        raise FsOpError(KIND_INVALID, f"not a file: {request.get('path')}")
    text = path.read_text(encoding="utf-8")
    mode = str(request.get("mode") or "exact_text").strip().lower()
    new_text = request.get("new_text")
    if not isinstance(new_text, str):
        raise FsOpError(KIND_INVALID, "new_text must be a string")

    if mode == "exact_text":
        old_text = request.get("old_text")
        if not isinstance(old_text, str) or not old_text:
            raise FsOpError(KIND_INVALID, "old_text must be a non-empty string")
        occurrences = text.count(old_text)
        if occurrences == 0:
            raise FsOpError(KIND_INVALID, "old_text not found")
        replace_all = bool(request.get("replace_all", False))
        if occurrences > 1 and not replace_all:
            raise FsOpError(
                KIND_INVALID,
                f"old_text matches {occurrences} times; pass replace_all or a longer unique snippet",
            )
        updated = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        return {"path": str(path), "occurrences": occurrences, "replacements": occurrences if replace_all else 1}

    if mode == "anchor_pair":
        start_anchor = str(request.get("start_anchor") or "")
        end_anchor = str(request.get("end_anchor") or "")
        if not start_anchor or not end_anchor:
            raise FsOpError(KIND_INVALID, "start_anchor and end_anchor are required")
        start = text.find(start_anchor)
        if start < 0:
            raise FsOpError(KIND_INVALID, "start_anchor not found")
        end = text.find(end_anchor, start + len(start_anchor))
        if end < 0:
            raise FsOpError(KIND_INVALID, "end_anchor not found after start_anchor")
        end += len(end_anchor)
        updated = text[:start] + new_text + text[end:]
        path.write_text(updated, encoding="utf-8")
        return {"path": str(path), "replaced_chars": end - start}

    if mode == "line_range":
        start_line = int(request.get("start_line") or 0)
        end_line = int(request.get("end_line") or 0)
        if start_line < 1 or end_line < start_line:
            raise FsOpError(KIND_INVALID, "start_line/end_line must satisfy 1 <= start <= end")
        lines = text.splitlines(keepends=True)
        if start_line > len(lines):
            raise FsOpError(KIND_INVALID, f"start_line exceeds the file ({len(lines)} lines)")
        end_line = min(end_line, len(lines))
        prefix = "".join(lines[: start_line - 1])
        suffix = "".join(lines[end_line:])
        updated = prefix + new_text + suffix
        path.write_text(updated, encoding="utf-8")
        return {"path": str(path), "start_line": start_line, "end_line": end_line}

    raise FsOpError(KIND_INVALID, f"unsupported edit mode: {mode}")


def _remove(request: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, Any]:
    raw_paths = request.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise FsOpError(KIND_INVALID, "paths must be a non-empty list")
    recursive = bool(request.get("recursive", False))
    allow_missing = bool(request.get("allow_missing", False))
    continue_on_error = bool(request.get("continue_on_error", False))
    removed: list[str] = []
    errors: list[dict[str, str]] = []
    for raw in raw_paths:
        try:
            path = _resolve(raw, roots)
            if not path.exists():
                if allow_missing:
                    continue
                raise FsOpError(KIND_INVALID, f"no such path: {raw}")
            if path.is_dir():
                if not recursive:
                    raise FsOpError(KIND_INVALID, f"is a directory (pass recursive): {raw}")
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(str(path))
        except FsOpError as exc:
            if not continue_on_error:
                raise
            errors.append({"path": str(raw), "error": str(exc)})
    return {"removed": removed, "errors": errors}


def _transfer(request: dict[str, Any], roots: tuple[Path, ...], *, move: bool) -> dict[str, Any]:
    raw_operations = request.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise FsOpError(KIND_INVALID, "operations must be a non-empty list")
    overwrite = bool(request.get("overwrite", False))
    create_parents = bool(request.get("create_parents", False))
    continue_on_error = bool(request.get("continue_on_error", False))
    completed: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for raw in raw_operations:
        operation = raw if isinstance(raw, dict) else {}
        try:
            source = _resolve(operation.get("source"), roots)
            destination = _resolve(operation.get("destination"), roots)
            if not source.exists():
                raise FsOpError(KIND_INVALID, f"no such source: {operation.get('source')}")
            if destination.exists() and not overwrite:
                raise FsOpError(KIND_INVALID, f"destination exists (pass overwrite): {operation.get('destination')}")
            if create_parents:
                destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir() and not move:
                if destination.exists() and not destination.is_dir():
                    raise FsOpError(KIND_INVALID, "destination is not a directory")
                shutil.copytree(source, destination, dirs_exist_ok=True)
            elif move:
                shutil.move(str(source), str(destination))
            else:
                shutil.copyfile(source, destination)
            completed.append({"source": str(source), "destination": str(destination)})
        except FsOpError as exc:
            if not continue_on_error:
                raise
            errors.append({"source": str(operation.get("source")), "error": str(exc)})
    return {"operations": completed, "errors": errors}


def _copy(request: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, Any]:
    return _transfer(request, roots, move=False)


def _move(request: dict[str, Any], roots: tuple[Path, ...]) -> dict[str, Any]:
    return _transfer(request, roots, move=True)


_OPS = {
    "read": _read,
    "write": _write,
    "replace": _replace,
    "edit": _edit,
    "remove": _remove,
    "copy": _copy,
    "move": _move,
    "list": _list,
    "stat": _stat,
    "search": _search,
}


def run(request: dict[str, Any], *, roots: tuple[Path, ...]) -> dict[str, Any]:
    op = str(request.get("op") or "").strip().lower()
    handler = _OPS.get(op)
    if handler is None:
        raise FsOpError(KIND_INVALID, f"unsupported op: {op}")
    return {"ok": True, "op": op, **handler(request, roots)}


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    # 走字节流而不是文本流：Windows 的 stdin/stdout 会按本地代码页解码，UTF-8 的
    # 中文路径与 JSON 里的反斜杠转义会被双字节编码吃掉一个字节（实测 "\\U" 变 "U"）。
    stdin = getattr(sys.stdin, "buffer", None)
    stdout = getattr(sys.stdout, "buffer", None)
    try:
        roots = _roots_from_argv(args)
        raw_bytes = stdin.read() if stdin is not None else sys.stdin.read().encode("utf-8")
        raw = raw_bytes.decode("utf-8", errors="replace")
        request = json.loads(raw) if raw.strip() else {}
        if not isinstance(request, dict):
            raise FsOpError(KIND_INVALID, "request must be an object")
        payload = run(request, roots=roots)
    except FsOpError as exc:
        payload = {"ok": False, "kind": exc.kind, "error": str(exc)}
    except json.JSONDecodeError as exc:
        payload = {"ok": False, "kind": KIND_INVALID, "error": f"invalid request json: {exc}"}
    except OSError as exc:
        payload = {"ok": False, "kind": KIND_IO, "error": str(exc)}
    encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    if stdout is not None:
        stdout.write(encoded)
        stdout.flush()
    else:
        sys.stdout.write(encoded.decode("utf-8"))
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
