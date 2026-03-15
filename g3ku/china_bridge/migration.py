from __future__ import annotations

from copy import deepcopy
from typing import Any


def migrate_legacy_china_channels(raw_channels: dict[str, Any] | None) -> dict[str, Any]:
    payload = deepcopy(dict(raw_channels or {}))
    legacy_qq = payload.get("qq")
    if isinstance(legacy_qq, dict) and "qqbot" not in payload:
        payload["qqbot"] = deepcopy(legacy_qq)
    legacy_feishu = payload.get("feishu")
    if isinstance(legacy_feishu, dict) and "feishuChina" not in payload and "feishu_china" not in payload:
        payload["feishuChina"] = deepcopy(legacy_feishu)
    return payload
