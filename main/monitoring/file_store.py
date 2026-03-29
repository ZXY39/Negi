from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


class TaskFileStore:
    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_task_dir_name(task_id: str) -> str:
        return str(task_id or '').strip().replace(':', '_').replace('/', '_').replace('\\', '_')

    def task_dir(self, task_id: str) -> Path:
        path = self.base_dir / self._safe_task_dir_name(task_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, path: str | Path, payload: dict[str, Any]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + '.tmp')
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(target)

    def read_json(self, path: str | Path) -> dict[str, Any] | None:
        target = Path(path)
        if not target.exists():
            return None
        return json.loads(target.read_text(encoding='utf-8'))

    def write_text(self, path: str | Path, content: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + '.tmp')
        temp.write_text(str(content or ''), encoding='utf-8')
        temp.replace(target)

    def read_text(self, path: str | Path) -> str | None:
        target = Path(path)
        if not target.exists():
            return None
        return target.read_text(encoding='utf-8')

    def delete_task_files(self, task_id: str) -> None:
        shutil.rmtree(self.base_dir / self._safe_task_dir_name(task_id), ignore_errors=True)
