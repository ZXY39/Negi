from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from g3ku.core.results import ContentEnvelope, ContentHandle

INLINE_CHAR_LIMIT = 1200
INLINE_LINE_LIMIT = 60
DEFAULT_OPEN_LINES = 80
MAX_OPEN_LINES = 200
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50
_HEAD_PREVIEW_LINES = 6
_TAIL_PREVIEW_LINES = 6
_PREVIEW_CHAR_LIMIT = 220


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return _json_dumps(value)
    return str(value or "")


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _preview_text(text: str, *, lines: int, max_chars: int = _PREVIEW_CHAR_LIMIT) -> str:
    selected = "\n".join(text.splitlines()[: max(1, int(lines or 1))]).strip()
    if len(selected) <= max_chars:
        return selected
    return selected[:max_chars].rstrip() + "..."


def _tail_preview_text(text: str, *, lines: int, max_chars: int = _PREVIEW_CHAR_LIMIT) -> str:
    selected = "\n".join(text.splitlines()[-max(1, int(lines or 1)) :]).strip()
    if len(selected) <= max_chars:
        return selected
    return selected[:max_chars].rstrip() + "..."


def _display_name(display_name: str, *, source_kind: str, fallback: str) -> str:
    return str(display_name or "").strip() or str(fallback or "").strip() or str(source_kind or "content")


def _runtime_task_id(runtime: dict[str, Any] | None) -> str:
    payload = runtime if isinstance(runtime, dict) else {}
    task_id = str(payload.get("task_id") or payload.get("project_id") or "").strip()
    if task_id:
        return task_id
    session_key = str(payload.get("session_key") or "").strip() or "shared"
    return f"adhoc:{session_key}"


def _runtime_node_id(runtime: dict[str, Any] | None) -> str | None:
    payload = runtime if isinstance(runtime, dict) else {}
    node_id = str(payload.get("node_id") or payload.get("unit_id") or "").strip()
    return node_id or None


def _content_summary(handle: ContentHandle) -> str:
    label = handle.display_name or handle.source_kind or "content"
    summary = (
        f"Externalized {label} "
        f"({int(handle.line_count or 0)} lines, {int(handle.char_count or 0)} chars). "
        f"Use content.search/open with ref={handle.ref}. Do not pass this ref as filesystem path."
    )
    if handle.origin_ref and handle.origin_ref != handle.ref:
        summary = f"{summary}\nOrigin ref: {handle.origin_ref}"
    if handle.head_preview:
        return f"{summary}\nHead preview:\n{handle.head_preview}"
    return summary


_ARTIFACT_REF_PATTERN = re.compile(r"artifact:artifact:[A-Za-z0-9_-]+")


def _extract_origin_ref(value: Any) -> str:
    envelope = parse_content_envelope(value)
    if envelope is not None and str(envelope.ref or "").strip():
        return str(envelope.ref or "").strip()

    payload: Any = value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                payload = json.loads(text)
            except Exception:
                payload = value

    refs: list[str] = []

    def _visit(item: Any) -> None:
        if isinstance(item, dict):
            ref = str(item.get("ref") or "").strip()
            if ref.startswith("artifact:artifact:"):
                refs.append(ref)
            handle = item.get("handle")
            if isinstance(handle, dict):
                handle_ref = str(handle.get("ref") or "").strip()
                if handle_ref.startswith("artifact:artifact:"):
                    refs.append(handle_ref)
            for nested in item.values():
                _visit(nested)
            return
        if isinstance(item, list):
            for nested in item:
                _visit(nested)
            return
        if isinstance(item, str):
            refs.extend(_ARTIFACT_REF_PATTERN.findall(item))

    _visit(payload)
    return refs[0] if refs else ""


def _looks_like_react_node_payload(value: Any, *, runtime: dict[str, Any] | None = None) -> bool:
    payload: dict[str, Any] | None = None
    if isinstance(value, dict):
        payload = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            return False
        try:
            parsed = json.loads(text)
        except Exception:
            return False
        if isinstance(parsed, dict):
            payload = parsed
    if not isinstance(payload, dict):
        return False
    required_keys = {"task_id", "node_id", "node_kind", "goal", "prompt"}
    if not required_keys.issubset(payload.keys()):
        return False
    if not isinstance(runtime, dict):
        return True
    runtime_task_id = str(runtime.get("task_id") or "").strip()
    runtime_node_id = str(runtime.get("node_id") or "").strip()
    runtime_node_kind = str(runtime.get("node_kind") or "").strip().lower()
    payload_task_id = str(payload.get("task_id") or "").strip()
    payload_node_id = str(payload.get("node_id") or "").strip()
    payload_node_kind = str(payload.get("node_kind") or "").strip().lower()
    if runtime_task_id and payload_task_id and runtime_task_id != payload_task_id:
        return False
    if runtime_node_id and payload_node_id and runtime_node_id != payload_node_id:
        return False
    if runtime_node_kind and payload_node_kind and runtime_node_kind != payload_node_kind:
        return False
    return True


def parse_content_envelope(value: Any) -> ContentEnvelope | None:
    if isinstance(value, ContentEnvelope):
        return value
    payload: dict[str, Any] | None = None
    if isinstance(value, dict):
        payload = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        if isinstance(parsed, dict):
            payload = parsed
    if not isinstance(payload, dict) or str(payload.get("type") or "").strip() != "content_ref":
        return None
    raw_handle = payload.get("handle")
    handle = None
    if isinstance(raw_handle, dict):
        handle = ContentHandle(
            ref=str(raw_handle.get("ref") or payload.get("ref") or "").strip(),
            artifact_id=str(raw_handle.get("artifact_id") or "").strip(),
            uri=str(raw_handle.get("uri") or "").strip(),
            source_kind=str(raw_handle.get("source_kind") or "text").strip() or "text",
            display_name=str(raw_handle.get("display_name") or "").strip(),
            mime_type=str(raw_handle.get("mime_type") or "text/plain").strip() or "text/plain",
            origin_ref=str(raw_handle.get("origin_ref") or "").strip(),
            size_bytes=int(raw_handle.get("size_bytes") or 0),
            line_count=int(raw_handle.get("line_count") or 0),
            char_count=int(raw_handle.get("char_count") or 0),
            head_preview=str(raw_handle.get("head_preview") or "").strip(),
            tail_preview=str(raw_handle.get("tail_preview") or "").strip(),
        )
    return ContentEnvelope(
        type="content_ref",
        summary=str(payload.get("summary") or "").strip(),
        ref=str(payload.get("ref") or getattr(handle, "ref", "") or "").strip(),
        handle=handle,
        next_actions=[str(item) for item in list(payload.get("next_actions") or []) if str(item or "").strip()],
    )


def content_summary_and_ref(value: Any) -> tuple[str, str]:
    envelope = parse_content_envelope(value)
    if envelope is not None:
        return envelope.summary, str(envelope.ref or "")
    if isinstance(value, (dict, list)):
        return _stringify(value), ""
    return str(value or ""), ""


def content_ref(value: Any) -> str:
    return content_summary_and_ref(value)[1]


class ContentNavigationService:
    def __init__(
        self,
        *,
        workspace: Path,
        allowed_dir: Path | None = None,
        artifact_store: Any = None,
        artifact_lookup: Any = None,
    ) -> None:
        self._workspace = Path(workspace).resolve()
        self._allowed_dir = Path(allowed_dir).resolve() if allowed_dir is not None else None
        self._artifact_store = artifact_store
        self._artifact_lookup = artifact_lookup

    def maybe_externalize_text(
        self,
        value: Any,
        *,
        runtime: dict[str, Any] | None = None,
        display_name: str = "",
        source_kind: str = "text",
        mime_type: str = "text/plain",
        force: bool = False,
    ) -> ContentEnvelope | None:
        envelope = parse_content_envelope(value)
        if envelope is not None:
            return envelope
        text = _stringify(value)
        if not text:
            return None
        if not force and len(text) <= INLINE_CHAR_LIMIT and _line_count(text) <= INLINE_LINE_LIMIT:
            return None
        display = _display_name(display_name, source_kind=source_kind, fallback=source_kind)
        origin_ref = _extract_origin_ref(value)
        handle = self._persist_text(
            text,
            runtime=runtime,
            display_name=display,
            source_kind=source_kind,
            mime_type=mime_type,
            origin_ref=origin_ref,
        )
        summary = _content_summary(handle)
        return ContentEnvelope(summary=summary, ref=handle.ref, handle=handle)

    def externalize_for_message(
        self,
        value: Any,
        *,
        runtime: dict[str, Any] | None = None,
        display_name: str = "",
        source_kind: str = "message",
        force: bool = False,
    ) -> Any:
        envelope = self.maybe_externalize_text(
            value,
            runtime=runtime,
            display_name=display_name,
            source_kind=source_kind,
            force=force,
        )
        if envelope is None:
            return value
        return _json_dumps(envelope.to_dict())

    def summarize_for_storage(
        self,
        value: Any,
        *,
        runtime: dict[str, Any] | None = None,
        display_name: str = "",
        source_kind: str = "content",
        force: bool = False,
    ) -> tuple[str, str]:
        envelope = self.maybe_externalize_text(
            value,
            runtime=runtime,
            display_name=display_name,
            source_kind=source_kind,
            force=force,
        )
        if envelope is not None:
            return envelope.summary, envelope.ref
        return content_summary_and_ref(value)

    def prepare_messages_for_model(
        self,
        messages: list[dict[str, Any]],
        *,
        runtime: dict[str, Any] | None = None,
        source_prefix: str = "message",
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        preserved_node_payload = False
        for index, message in enumerate(list(messages or [])):
            if not isinstance(message, dict):
                prepared.append(message)
                continue
            role = str(message.get("role") or "").strip().lower()
            if role not in {"user", "assistant", "tool"}:
                prepared.append(dict(message))
                continue
            updated = dict(message)
            if "content" in updated:
                preserve_inline = (
                    not preserved_node_payload
                    and role == "user"
                    and str(source_prefix or "").strip().lower() == "react"
                    and _looks_like_react_node_payload(updated.get("content"), runtime=runtime)
                )
                if preserve_inline:
                    preserved_node_payload = True
                else:
                    updated["content"] = self.externalize_for_message(
                        updated.get("content"),
                        runtime=runtime,
                        display_name=f"{source_prefix}-{role}-{index + 1}",
                        source_kind=f"{source_prefix}_{role}",
                    )
            prepared.append(updated)
        return prepared

    def describe(self, *, ref: str | None = None, path: str | None = None) -> dict[str, Any]:
        text, handle = self._resolve(ref=ref, path=path)
        return {
            "ok": True,
            "ref": handle.ref,
            "handle": handle.to_dict(),
            "summary": _content_summary(handle),
            "size_bytes": handle.size_bytes,
            "line_count": handle.line_count,
            "char_count": handle.char_count,
        }

    def head(self, *, ref: str | None = None, path: str | None = None, lines: int = DEFAULT_OPEN_LINES) -> dict[str, Any]:
        return self._excerpt(ref=ref, path=path, start_line=1, end_line=max(1, min(int(lines or DEFAULT_OPEN_LINES), MAX_OPEN_LINES)))

    def tail(self, *, ref: str | None = None, path: str | None = None, lines: int = DEFAULT_OPEN_LINES) -> dict[str, Any]:
        text, handle = self._resolve(ref=ref, path=path)
        all_lines = text.splitlines()
        size = max(1, min(int(lines or DEFAULT_OPEN_LINES), MAX_OPEN_LINES))
        start_line = max(1, len(all_lines) - size + 1)
        return self._excerpt(ref=ref, path=path, start_line=start_line, end_line=len(all_lines))

    def open(
        self,
        *,
        ref: str | None = None,
        path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        around_line: int | None = None,
        window: int | None = None,
    ) -> dict[str, Any]:
        if around_line is not None:
            span = max(1, min(int(window or DEFAULT_OPEN_LINES), MAX_OPEN_LINES))
            half = max(1, span // 2)
            start_line = max(1, int(around_line) - half)
            end_line = max(start_line, int(around_line) + half)
        start = max(1, int(start_line or 1))
        finish = max(start, int(end_line or (start + DEFAULT_OPEN_LINES - 1)))
        if (finish - start + 1) > MAX_OPEN_LINES:
            finish = start + MAX_OPEN_LINES - 1
        return self._excerpt(ref=ref, path=path, start_line=start, end_line=finish)

    def search(
        self,
        *,
        query: str,
        ref: str | None = None,
        path: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
        before: int = 2,
        after: int = 2,
    ) -> dict[str, Any]:
        text, handle = self._resolve(ref=ref, path=path)
        needle = str(query or "").strip()
        if not needle:
            return {"ok": False, "error": "query is required"}
        lines = text.splitlines()
        results: list[dict[str, Any]] = []
        max_hits = max(1, min(int(limit or DEFAULT_SEARCH_LIMIT), MAX_SEARCH_LIMIT))
        before_count = max(0, min(int(before or 0), 10))
        after_count = max(0, min(int(after or 0), 10))
        try:
            pattern = re.compile(needle, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(needle), re.IGNORECASE)
        for index, line in enumerate(lines):
            if not pattern.search(line):
                continue
            start = max(0, index - before_count)
            end = min(len(lines), index + after_count + 1)
            results.append(
                {
                    "line": index + 1,
                    "preview": "\n".join(lines[start:end]).strip(),
                }
            )
            if len(results) >= max_hits:
                break
        return {
            "ok": True,
            "ref": handle.ref,
            "handle": handle.to_dict(),
            "query": needle,
            "hits": results,
            "count": len(results),
        }

    def _excerpt(self, *, ref: str | None = None, path: str | None = None, start_line: int, end_line: int) -> dict[str, Any]:
        text, handle = self._resolve(ref=ref, path=path)
        lines = text.splitlines()
        start = max(1, int(start_line or 1))
        finish = max(start, int(end_line or start))
        excerpt = "\n".join(lines[start - 1 : finish]).strip()
        return {
            "ok": True,
            "ref": handle.ref,
            "handle": handle.to_dict(),
            "start_line": start,
            "end_line": min(finish, len(lines)),
            "excerpt": excerpt,
        }

    def _resolve(self, *, ref: str | None = None, path: str | None = None) -> tuple[str, ContentHandle]:
        if path:
            file_path = self._resolve_workspace_path(path)
            if not file_path.exists():
                raise FileNotFoundError(f"path not found: {path}")
            if not file_path.is_file():
                raise ValueError(f"path is not a file: {path}")
            text = file_path.read_text(encoding="utf-8")
            try:
                ref_path = str(file_path.relative_to(self._workspace)).replace("\\", "/")
            except ValueError:
                ref_path = str(file_path)
            return text, self._build_handle(
                ref=f"path:{ref_path}",
                artifact_id="",
                uri=str(file_path),
                source_kind="file_path",
                display_name=file_path.name,
                mime_type="text/plain",
                origin_ref="",
                text=text,
            )

        normalized_ref = self._normalize_ref(ref)
        if normalized_ref.startswith("path:"):
            return self._resolve(path=normalized_ref[5:])
        if not normalized_ref.startswith("artifact:"):
            raise ValueError(f"unsupported content ref: {normalized_ref or '<empty>'}")
        artifact_id = normalized_ref.split(":", 1)[1]
        artifact = self._lookup_artifact(artifact_id)
        if artifact is None or not getattr(artifact, "path", None):
            raise FileNotFoundError(f"artifact not found: {artifact_id}")
        artifact_path = Path(str(artifact.path))
        text = artifact_path.read_text(encoding="utf-8") if artifact_path.exists() else ""
        return text, self._build_handle(
            ref=normalized_ref,
            artifact_id=artifact_id,
            uri=str(artifact_path),
            source_kind=str(getattr(artifact, "kind", "") or "artifact"),
            display_name=str(getattr(artifact, "title", "") or artifact_path.name),
            mime_type=str(getattr(artifact, "mime_type", "") or "text/plain"),
            origin_ref="",
            text=text,
        )

    def _normalize_ref(self, ref: str | None) -> str:
        envelope = parse_content_envelope(ref)
        if envelope is not None:
            return str(envelope.ref or "")
        return str(ref or "").strip()

    def _lookup_artifact(self, artifact_id: str) -> Any | None:
        lookup = self._artifact_lookup
        if lookup is None and self._artifact_store is not None and hasattr(self._artifact_store, "get_artifact"):
            lookup = self._artifact_store.get_artifact
        if lookup is None:
            return None
        if callable(lookup):
            return lookup(artifact_id)
        if hasattr(lookup, "get_artifact"):
            return lookup.get_artifact(artifact_id)
        return None

    def _persist_text(
        self,
        text: str,
        *,
        runtime: dict[str, Any] | None,
        display_name: str,
        source_kind: str,
        mime_type: str,
        origin_ref: str,
    ) -> ContentHandle:
        artifact = None
        if self._artifact_store is not None and hasattr(self._artifact_store, "create_text_artifact"):
            artifact = self._artifact_store.create_text_artifact(
                task_id=_runtime_task_id(runtime),
                node_id=_runtime_node_id(runtime),
                kind=source_kind,
                title=display_name,
                content=text,
                extension=".txt",
                mime_type=mime_type,
            )
        ref = f"artifact:{artifact.artifact_id}" if artifact is not None else ""
        uri = str(getattr(artifact, "path", "") or "")
        artifact_id = str(getattr(artifact, "artifact_id", "") or "")
        return self._build_handle(
            ref=ref,
            artifact_id=artifact_id,
            uri=uri,
            source_kind=source_kind,
            display_name=display_name,
            mime_type=mime_type,
            origin_ref=(origin_ref if origin_ref != ref else ""),
            text=text,
        )

    def _resolve_workspace_path(self, path: str) -> Path:
        raw = str(path or "").strip()
        if raw.startswith("artifact:"):
            raise ValueError(f"content ref must be passed via ref, not path: {path}")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"relative path is not allowed; provide absolute path: {path}")
        resolved = candidate.resolve()
        if self._allowed_dir is not None:
            try:
                resolved.relative_to(self._allowed_dir)
            except ValueError as exc:
                if self._allowed_dir == self._workspace:
                    raise PermissionError(f"path outside workspace: {path}") from exc
                raise PermissionError(f"path outside allowed directory: {path}") from exc
        return resolved

    @staticmethod
    def _build_handle(
        *,
        ref: str,
        artifact_id: str,
        uri: str,
        source_kind: str,
        display_name: str,
        mime_type: str,
        origin_ref: str,
        text: str,
    ) -> ContentHandle:
        encoded = text.encode("utf-8")
        return ContentHandle(
            ref=ref,
            artifact_id=artifact_id,
            uri=uri,
            source_kind=source_kind,
            display_name=display_name,
            mime_type=mime_type,
            origin_ref=origin_ref,
            size_bytes=len(encoded),
            line_count=_line_count(text),
            char_count=len(text),
            head_preview=_preview_text(text, lines=_HEAD_PREVIEW_LINES),
            tail_preview=_tail_preview_text(text, lines=_TAIL_PREVIEW_LINES),
        )
