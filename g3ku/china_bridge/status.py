from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from g3ku.china_bridge.models import ChinaBridgeState


class ChinaBridgeStatusStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, state: ChinaBridgeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")

    def read(self) -> ChinaBridgeState:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return ChinaBridgeState()
        return ChinaBridgeState(**{key: value for key, value in payload.items() if key in ChinaBridgeState.__dataclass_fields__})
