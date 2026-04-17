from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from g3ku.agent.markdown_memory import note_file_name
from g3ku.agent.memory_catalog_bridge import MemoryCatalogBridge


@dataclass(slots=True)
class MemoryQueueRequest:
    op: str
    decision_source: str
    payload_text: str
    created_at: str
    request_id: str = ""
    trigger_source: str = ""
    session_key: str = ""


@dataclass(slots=True)
class MemoryBatch:
    op: str
    items: list[MemoryQueueRequest] = field(default_factory=list)


class MemoryManager:
    def __init__(self, workspace: Path, config: Any):
        self.workspace = Path(workspace)
        self.config = config
        self.mem_dir = self.workspace / "memory"
        self.memory_file = self.workspace / str(config.document.memory_file)
        self.notes_dir = self.workspace / str(config.document.notes_dir)
        self.queue_file = self.workspace / str(config.queue.queue_file)
        self.ops_file = self.workspace / str(config.queue.ops_file)
        self._io_lock = asyncio.Lock()
        self._worker_task: asyncio.Task[None] | None = None
        self._catalog_bridge = MemoryCatalogBridge(self.workspace, config)
        self.store = getattr(self._catalog_bridge, "store", None)
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self.mem_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_file.exists():
            self.memory_file.write_text("", encoding="utf-8")
        if not self.queue_file.exists():
            self.queue_file.write_text("", encoding="utf-8")
        if not self.ops_file.exists():
            self.ops_file.write_text("", encoding="utf-8")

    def snapshot_text(self, **_: Any) -> str:
        if not self.memory_file.exists():
            return ""
        return self.memory_file.read_text(encoding="utf-8").strip()

    async def _append_queue_request(self, request: MemoryQueueRequest) -> None:
        line = json.dumps(asdict(request), ensure_ascii=False)
        async with self._io_lock:
            with self.queue_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    async def collect_due_batch(self, *, now_iso: str) -> MemoryBatch | None:
        if not self.queue_file.exists():
            return None
        async with self._io_lock:
            rows = [
                json.loads(line)
                for line in self.queue_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        if not rows:
            return None

        first = MemoryQueueRequest(**rows[0])
        max_chars = int(getattr(self.config.queue, "batch_max_chars", 50000) or 50000)
        current_chars = 0
        hit_char_boundary = False
        items: list[MemoryQueueRequest] = []
        for row in rows:
            candidate = MemoryQueueRequest(**row)
            if candidate.op != first.op:
                break
            next_chars = current_chars + len(candidate.payload_text)
            if items and next_chars > max_chars:
                hit_char_boundary = True
                break
            current_chars = next_chars
            items.append(candidate)

        waited_seconds = self._seconds_since(first.created_at, now_iso)
        if (
            not hit_char_boundary
            and current_chars < max_chars
            and waited_seconds < int(getattr(self.config.queue, "max_wait_seconds", 3) or 3)
        ):
            return None
        return MemoryBatch(op=first.op, items=items)

    async def enqueue_write_request(
        self,
        *,
        session_key: str,
        decision_source: str,
        payload_text: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        request = MemoryQueueRequest(
            op="write",
            decision_source=str(decision_source or "").strip() or "user",
            payload_text=str(payload_text or "").strip(),
            created_at=self._now_iso(),
            session_key=str(session_key or "").strip(),
            trigger_source=str(trigger_source or "").strip(),
            request_id=self._request_id("write"),
        )
        await self._append_queue_request(request)
        return {"ok": True, "request_id": request.request_id, "status": "queued"}

    async def enqueue_delete_request(
        self,
        *,
        session_key: str,
        decision_source: str,
        payload_text: str,
        trigger_source: str,
    ) -> dict[str, Any]:
        request = MemoryQueueRequest(
            op="delete",
            decision_source=str(decision_source or "").strip() or "user",
            payload_text=str(payload_text or "").strip(),
            created_at=self._now_iso(),
            session_key=str(session_key or "").strip(),
            trigger_source=str(trigger_source or "").strip(),
            request_id=self._request_id("delete"),
        )
        await self._append_queue_request(request)
        return {"ok": True, "request_id": request.request_id, "status": "queued"}

    def load_note(self, ref: str) -> str:
        path = self.notes_dir / note_file_name(ref)
        if not path.exists():
            raise FileNotFoundError(f"memory note not found: {ref}")
        return path.read_text(encoding="utf-8")

    async def sync_catalog(self, service: Any) -> Any:
        return await self._catalog_bridge.sync_catalog(service)

    async def ensure_catalog_bootstrap(self, service: Any) -> Any:
        return await self._catalog_bridge.ensure_catalog_bootstrap(service)

    def close(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            self._worker_task = None
        self._catalog_bridge.close()

    @staticmethod
    def _request_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().astimezone().isoformat()

    @staticmethod
    def _seconds_since(start_iso: str, end_iso: str) -> int:
        start = datetime.fromisoformat(str(start_iso or "").strip())
        end = datetime.fromisoformat(str(end_iso or "").strip())
        return max(0, int((end - start).total_seconds()))
