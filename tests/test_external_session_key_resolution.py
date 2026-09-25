"""external_key 路由歧义的判据。

同一个 external_key 可以合法地对应多个会话：QQ 开放平台的 openid 按 AppID 隔离，
而 bridge_id 改名（单号 → ``qq-official-<appId>``）会让新旧两条目同时存在。
解析必须确定且可见，不能靠字典插入序静默挑一条——挑中旧会话就是静默丢投递。
"""

from __future__ import annotations

from pathlib import Path

from g3ku.runtime.external_sessions import ExternalSessionRegistry
from g3ku.runtime.session_keys import build_external_session_key

EXTERNAL_KEY = "qq:c2c:EB6C8D4341C1238A627FF73CBE540DAE"


def _registry(tmp_path: Path) -> ExternalSessionRegistry:
    return ExternalSessionRegistry(tmp_path)


def test_duplicate_external_key_resolves_to_the_newest_session(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    legacy, created_old = registry.resolve_or_create(bridge_id="qq-official", external_key=EXTERNAL_KEY)
    assert created_old
    moved, created_new = registry.resolve_or_create(bridge_id="qq-official-1903529517", external_key=EXTERNAL_KEY)
    assert created_new
    assert moved.session_key == build_external_session_key(
        bridge_id="qq-official-1903529517", external_key=EXTERNAL_KEY
    )
    # 插入序的第一条是旧会话；按 created_at 取最新才不会再投给已无桥消费的目标。
    assert next(iter(registry._entries)) == legacy.session_key

    assert registry.find_by_any_key(EXTERNAL_KEY).session_key == moved.session_key


def test_session_key_lookup_is_unaffected_by_the_tie_break(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    old, _ = registry.resolve_or_create(bridge_id="qq-official", external_key=EXTERNAL_KEY)
    new, _ = registry.resolve_or_create(bridge_id="qq-official-1903529517", external_key=EXTERNAL_KEY)

    assert registry.find_by_any_key(old.session_key).session_key == old.session_key
    assert registry.find_by_any_key(new.session_key).session_key == new.session_key
    assert registry.find_by_any_key("qq:c2c:unknown") is None
    assert registry.find_by_any_key("  ") is None


def test_single_match_does_not_log_a_warning(tmp_path: Path) -> None:
    from loguru import logger

    registry = _registry(tmp_path)
    entry, _ = registry.resolve_or_create(bridge_id="qq-official", external_key="qq:c2c:single")
    seen: list[str] = []
    sink_id = logger.add(lambda message: seen.append(str(message)), level="WARNING")
    try:
        assert registry.find_by_any_key("qq:c2c:single").session_key == entry.session_key
    finally:
        logger.remove(sink_id)
    assert seen == []
