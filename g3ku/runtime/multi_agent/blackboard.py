from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from g3ku.runtime.multi_agent.state import BlackboardRef


class BlackboardStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def alloc_run_dir(self, *, session_key: str, run_id: str | None = None) -> Path:
        safe_session = self._slug(session_key, fallback="session")
        run_token = str(run_id or uuid.uuid4().hex[:12]).strip() or uuid.uuid4().hex[:12]
        run_dir = self.root / safe_session / run_token
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def write_artifact(
        self,
        *,
        run_dir: Path,
        name: str,
        content: Any,
        extension: str | None = None,
        content_type: str | None = None,
    ) -> BlackboardRef:
        run_path = Path(run_dir)
        run_path.mkdir(parents=True, exist_ok=True)
        stem = self._slug(name, fallback="artifact")
        payload, suffix, mime = self._normalize_payload(content, extension=extension, content_type=content_type)
        path = self._unique_path(run_path, stem, suffix)
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(payload, encoding="utf-8")
        relative = path.relative_to(self.root).as_posix()
        return BlackboardRef(
            run_id=run_path.name,
            label=name,
            relative_path=relative,
            abs_path=str(path.resolve()),
            content_type=mime,
        )

    def read_artifact(self, ref: BlackboardRef | dict[str, Any] | str) -> str:
        path = self._resolve_path(ref)
        return path.read_text(encoding="utf-8")

    def _resolve_path(self, ref: BlackboardRef | dict[str, Any] | str) -> Path:
        if isinstance(ref, BlackboardRef):
            candidate = ref.abs_path or ref.relative_path
        elif isinstance(ref, dict):
            candidate = str(ref.get("abs_path") or ref.get("relative_path") or "")
        else:
            candidate = str(ref or "")
        path = Path(candidate)
        if not path.is_absolute():
            path = self.root / path
        return path

    def _normalize_payload(self, content: Any, *, extension: str | None, content_type: str | None) -> tuple[str | bytes, str, str]:
        ext = str(extension or "").strip()
        mime = str(content_type or "").strip()
        if isinstance(content, bytes):
            return content, ext or ".bin", mime or "application/octet-stream"
        if isinstance(content, (dict, list)):
            return json.dumps(content, ensure_ascii=False, indent=2), ext or ".json", mime or "application/json"
        return str(content or ""), ext or ".md", mime or "text/markdown"

    def _unique_path(self, run_dir: Path, stem: str, suffix: str) -> Path:
        candidate = run_dir / f"{stem}{suffix}"
        if not candidate.exists():
            return candidate
        return run_dir / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"

    @staticmethod
    def _slug(value: str, *, fallback: str) -> str:
        text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
        return text[:80] or fallback

