"""Configuration loading for the QQ/OneBot bridge."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class OnebotConfig:
    ws_url: str = "ws://127.0.0.1:3001"
    http_url: str = "http://127.0.0.1:3000"


@dataclass(slots=True)
class G3kuConfig:
    base_url: str = "http://127.0.0.1:18790"
    token: str = ""


@dataclass(slots=True)
class BehaviorConfig:
    group_require_at: bool = True
    final_only: bool = False
    progress_min_interval_seconds: float = 5.0
    progress_max_lines_per_message: int = 3
    max_message_length: int = 4000
    bot_user_id: int = 0
    reconnect_backoff_seconds: float = 3.0


@dataclass(slots=True)
class BridgeConfig:
    onebot: OnebotConfig = field(default_factory=OnebotConfig)
    g3ku: G3kuConfig = field(default_factory=G3kuConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)


def load_bridge_config(path: Path | str) -> BridgeConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    onebot_raw = raw.get("onebot") or {}
    g3ku_raw = raw.get("g3ku") or {}
    behavior_raw = raw.get("behavior") or {}
    return BridgeConfig(
        onebot=OnebotConfig(
            ws_url=str(onebot_raw.get("ws_url") or OnebotConfig.ws_url),
            http_url=str(onebot_raw.get("http_url") or OnebotConfig.http_url),
        ),
        g3ku=G3kuConfig(
            base_url=str(g3ku_raw.get("base_url") or G3kuConfig.base_url).rstrip("/"),
            token=str(g3ku_raw.get("token") or ""),
        ),
        behavior=BehaviorConfig(
            group_require_at=bool(behavior_raw.get("group_require_at", True)),
            final_only=bool(behavior_raw.get("final_only", False)),
            progress_min_interval_seconds=float(behavior_raw.get("progress_min_interval_seconds", 5.0)),
            progress_max_lines_per_message=int(behavior_raw.get("progress_max_lines_per_message", 3)),
            max_message_length=int(behavior_raw.get("max_message_length", 4000)),
            bot_user_id=int(behavior_raw.get("bot_user_id", 0) or 0),
            reconnect_backoff_seconds=float(behavior_raw.get("reconnect_backoff_seconds", 3.0)),
        ),
    )
