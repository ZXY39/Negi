"""「当操作员」授权的信任链：白名单 → owner → 档位/记忆/任务，撤掉后下一条降级。

契约见 ``docs/FIX_PLAN_external-token-operator-trust.md`` 与
``docs/architecture/sandbox-execution-plane.md``「信任分级」。这里钉三件事：

* 网关固定身份进白名单后，会话按 owner 处理（档位放开、写类工具可用）；
* 白名单撤掉后，**下一条消息**就降回第三方（信任逐条 min 合并，不是一次写死）；
* 未授权的桥即使自报同一个身份，仍然是第三方（白名单是服务端配置，不是自报即可命中）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# 先导入 main：`session_keys` ↔ `main.runtime.react_loop` 的既有循环要求这个顺序
# （生产入口也是先装 main 再进 runtime）。
import main  # noqa: F401
from g3ku.runtime.external_sessions import (
    get_external_session_registry,
    reset_external_session_registry,
)
from g3ku.runtime.session_trust import (
    TRUST_OWNER,
    TRUST_THIRD_PARTY_DM,
    note_inbound_session_shape,
    session_trust,
)
from g3ku.sandbox.policy import TIER_HOST_FULL, session_policy

# 网关每轮自报的身份。写成字面量而不是 import 常量：本文件要在未改动的树上先跑红，
# 模块级 import 一个还不存在的名字会变成 collection 错误，那不是有效红灯。
GATEWAY_SENDER = "negi-mcp"


@pytest.fixture
def registry(tmp_path: Path):
    reset_external_session_registry()
    reg = get_external_session_registry(tmp_path)
    try:
        yield reg
    finally:
        reset_external_session_registry()


def _new_session(reg, external_key: str) -> str:
    entry, _created = reg.resolve_or_create(bridge_id="qoder", external_key=external_key)
    return entry.session_key


def test_gateway_sender_constant_matches_plan() -> None:
    from g3ku.runtime.session_trust import GATEWAY_SENDER_ID

    assert GATEWAY_SENDER_ID == GATEWAY_SENDER


def test_trusted_gateway_sender_makes_session_owner(registry) -> None:
    key = _new_session(registry, "mcp:operator-grant")

    trust = note_inbound_session_shape(
        key, sender_id=GATEWAY_SENDER, trusted_senders=[GATEWAY_SENDER]
    )

    assert trust == TRUST_OWNER
    assert session_trust(key) == TRUST_OWNER
    policy = session_policy(key)
    assert policy.tier == TIER_HOST_FULL
    assert policy.tool_allowed("filesystem_write") is True
    assert policy.tool_allowed("create_async_task") is True
    assert policy.blocked_tool_names(["filesystem_write", "create_async_task"]) == []


def test_revoking_grant_downgrades_on_next_message(registry) -> None:
    key = _new_session(registry, "mcp:operator-revoke")
    note_inbound_session_shape(
        key, sender_id=GATEWAY_SENDER, trusted_senders=[GATEWAY_SENDER]
    )
    assert session_trust(key) == TRUST_OWNER

    downgraded = note_inbound_session_shape(key, sender_id=GATEWAY_SENDER, trusted_senders=[])

    assert downgraded == TRUST_THIRD_PARTY_DM
    assert session_trust(key) == TRUST_THIRD_PARTY_DM
    policy = session_policy(key)
    assert policy.restricted is True
    # 受限档默认允许工作目录内写文件；派发与命令行仍不可用。
    assert policy.tool_allowed("filesystem_write") is True
    assert policy.tool_allowed("exec") is False
    assert policy.tool_allowed("create_async_task") is False


def test_forged_gateway_sender_without_grant_stays_third_party(registry) -> None:
    key = _new_session(registry, "mcp:forged-sender")

    trust = note_inbound_session_shape(key, sender_id=GATEWAY_SENDER, trusted_senders=[])

    assert trust == TRUST_THIRD_PARTY_DM
    assert session_trust(key) == TRUST_THIRD_PARTY_DM
    assert session_policy(key).restricted is True


def test_last_proactive_at_persists_across_registry_reload(registry) -> None:
    from g3ku.runtime.external_sessions import ExternalSessionRegistry

    key = _new_session(registry, "qq:group:proactive")
    assert registry.last_proactive_at(key) == ""

    registry.note_proactive_outbound(key, at="2026-10-30T12:00:00+08:00")
    assert registry.last_proactive_at(key) == "2026-10-30T12:00:00+08:00"

    reloaded = ExternalSessionRegistry(registry.workspace)
    assert reloaded.last_proactive_at(key) == "2026-10-30T12:00:00+08:00"
