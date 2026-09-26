from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from g3ku.config.loader import (
    _load_balance_groups_payload,
    _managed_models_payload,
    _runtime_config_payload,
)
from g3ku.config.schema import (
    GROUP_MAX_RETRY_ROUNDS_LIMIT,
    Config,
    ModelLoadBalanceGroup,
    ModelRouteEntry,
    RoleModelRoutingConfig,
)


def _catalog() -> list[dict[str, Any]]:
    # 带 llmConfigId：保存后的 config.json 不落 providerModel/apiKey（密钥走 secret
    # overlay），只靠绑定记录引用；round-trip 测试必须按落盘后的形状构造。
    return [
        {"key": "m_a", "llmConfigId": "rec_m_a", "contextWindowTokens": 200000},
        {"key": "m_b", "llmConfigId": "rec_m_b", "contextWindowTokens": 200000},
        {"key": "m_x", "llmConfigId": "rec_m_x", "contextWindowTokens": 200000},
        {"key": "m_off", "llmConfigId": "rec_m_off", "contextWindowTokens": 200000, "enabled": False},
    ]


def _config_payload(**overrides: Any) -> dict[str, Any]:
    models: dict[str, Any] = {"catalog": _catalog(), "roles": {"ceo": ["m_x"], "execution": ["m_a"], "inspection": ["m_b"], "memory": []}}
    models.update(overrides)
    return {"agents": {"defaults": {"workspace": "."}}, "models": models}


def test_legacy_string_chain_normalizes_to_route_entries() -> None:
    cfg = Config.model_validate(_config_payload())

    assert cfg.models.roles.execution == [ModelRouteEntry(type="model", model_key="m_a")]
    assert cfg.get_role_model_keys("execution") == ["m_a"]
    # 旧配置读出后仍是字符串候选视图，且 chain 解析照旧可用。
    assert [target.model_key for target in cfg.get_scope_model_chain("execution")] == ["m_a"]


def test_route_entries_accept_camel_and_snake_and_keep_order() -> None:
    roles = RoleModelRoutingConfig.model_validate(
        {
            "execution": [
                {"type": "load_balance", "groupKey": "g1"},
                {"type": "model", "model_key": "m_x"},
            ]
        }
    )

    assert [entry.type for entry in roles.execution] == ["load_balance", "model"]
    assert roles.execution[0].group_key == "g1"
    assert roles.execution[1].model_key == "m_x"


def test_group_member_order_expands_into_candidate_view() -> None:
    cfg = Config.model_validate(
        _config_payload(
            loadBalanceGroups={"g1": {"modelKeys": ["m_b", "m_a"]}},
            roles={
                "ceo": ["m_x"],
                "execution": [{"type": "load_balance", "groupKey": "g1"}, {"type": "model", "modelKey": "m_x"}],
                "inspection": [{"type": "load_balance", "groupKey": "g1"}],
                "memory": [],
            },
        )
    )

    # 候选视图展开组成员并保持声明顺序；链上后面的 direct 不重复出现。
    assert cfg.get_role_model_keys("execution") == ["m_b", "m_a", "m_x"]
    assert cfg.get_role_model_keys("inspection") == ["m_b", "m_a"]
    assert cfg.get_load_balance_group("g1").model_keys == ["m_b", "m_a"]


def test_disabled_group_drops_whole_route_entry() -> None:
    cfg = Config.model_validate(
        _config_payload(
            loadBalanceGroups={"g1": {"enabled": False, "modelKeys": ["m_a", "m_b"]}},
            roles={
                "ceo": ["m_x"],
                "execution": [{"type": "load_balance", "groupKey": "g1"}, {"type": "model", "modelKey": "m_x"}],
                "inspection": ["m_x"],
                "memory": [],
            },
        )
    )

    assert cfg.get_role_model_keys("execution") == ["m_x"]


def test_group_defaults_and_bounds_on_max_retry_rounds() -> None:
    group = ModelLoadBalanceGroup.model_validate({"modelKeys": ["m_a"]})
    assert group.max_retry_rounds == 1
    assert group.enabled is True

    with pytest.raises(ValueError, match="maxRetryRounds"):
        ModelLoadBalanceGroup.model_validate({"modelKeys": ["m_a"], "maxRetryRounds": GROUP_MAX_RETRY_ROUNDS_LIMIT + 1})
    with pytest.raises(ValueError, match="maxRetryRounds"):
        ModelLoadBalanceGroup.model_validate({"modelKeys": ["m_a"], "maxRetryRounds": 0})


def test_group_duplicate_member_is_rejected_not_deduped() -> None:
    with pytest.raises(ValueError, match="appears twice"):
        ModelLoadBalanceGroup.model_validate({"modelKeys": ["m_a", "m_a"]})


def test_route_entry_shape_validation() -> None:
    with pytest.raises(ValueError, match="requires modelKey"):
        ModelRouteEntry.model_validate({"type": "model"})
    with pytest.raises(ValueError, match="must not set groupKey"):
        ModelRouteEntry.model_validate({"type": "model", "modelKey": "m_a", "groupKey": "g1"})
    with pytest.raises(ValueError, match="type must be one of"):
        ModelRouteEntry.model_validate({"type": "round_robin", "modelKey": "m_a"})


def test_ceo_and_memory_reject_group_entries() -> None:
    for scope in ("ceo", "memory"):
        roles = {"ceo": ["m_x"], "execution": ["m_a"], "inspection": ["m_b"], "memory": ["m_x"]}
        roles[scope] = [{"type": "load_balance", "groupKey": "g1"}]
        with pytest.raises(ValueError, match="负载均衡组当前仅支持 execution/inspection"):
            Config.model_validate(_config_payload(loadBalanceGroups={"g1": {"modelKeys": ["m_a"]}}, roles=roles))


def test_group_reference_and_member_validation_messages() -> None:
    base = {"ceo": ["m_x"], "execution": [{"type": "load_balance", "groupKey": "missing"}], "inspection": ["m_b"], "memory": []}
    with pytest.raises(ValueError, match="unknown group key"):
        Config.model_validate(_config_payload(loadBalanceGroups={"g1": {"modelKeys": ["m_a"]}}, roles=base))

    with pytest.raises(ValueError, match="must configure modelKeys"):
        Config.model_validate(_config_payload(loadBalanceGroups={"g1": {"modelKeys": []}}, roles=base))

    disabled_member = {"ceo": ["m_x"], "execution": [{"type": "load_balance", "groupKey": "g1"}], "inspection": ["m_b"], "memory": []}
    with pytest.raises(ValueError, match="references disabled model key: m_off"):
        Config.model_validate(
            _config_payload(loadBalanceGroups={"g1": {"modelKeys": ["m_off"]}}, roles=disabled_member)
        )


def test_group_key_colliding_with_model_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="collides with a model key"):
        Config.model_validate(
            _config_payload(
                loadBalanceGroups={"m_a": {"modelKeys": ["m_b"]}},
                roles={
                    "ceo": ["m_x"],
                    "execution": [{"type": "load_balance", "groupKey": "m_a"}],
                    "inspection": ["m_b"],
                    "memory": [],
                },
            )
        )


def test_serialization_keeps_legacy_flat_shape_and_switches_only_for_groups() -> None:
    legacy = Config.model_validate(_config_payload())
    _catalog_payload, roles_payload = _managed_models_payload(legacy)
    assert roles_payload["execution"] == ["m_a"]
    assert "loadBalanceGroups" not in _runtime_config_payload(legacy)["models"]

    with_group = Config.model_validate(
        _config_payload(
            loadBalanceGroups={"g1": {"modelKeys": ["m_a", "m_b"], "maxRetryRounds": 2}},
            roles={
                "ceo": ["m_x"],
                "execution": [{"type": "load_balance", "groupKey": "g1"}, {"type": "model", "modelKey": "m_x"}],
                "inspection": ["m_b"],
                "memory": [],
            },
        )
    )
    _catalog_payload, roles_payload = _managed_models_payload(with_group)
    assert roles_payload["execution"] == [
        {"type": "load_balance", "groupKey": "g1"},
        {"type": "model", "modelKey": "m_x"},
    ]
    # 没出现组的链保持旧形状，不因为别的链用了组而被改写。
    assert roles_payload["inspection"] == ["m_b"]
    assert _load_balance_groups_payload(with_group) == {
        "g1": {"enabled": True, "maxRetryRounds": 2, "modelKeys": ["m_a", "m_b"]}
    }


def test_round_trip_through_saved_json(tmp_path: Path) -> None:
    with_group = Config.model_validate(
        _config_payload(
            loadBalanceGroups={"g1": {"modelKeys": ["m_a", "m_b"]}},
            roles={
                "ceo": ["m_x"],
                "execution": [{"type": "load_balance", "groupKey": "g1"}],
                "inspection": ["m_b"],
                "memory": [],
            },
        )
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_runtime_config_payload(with_group), ensure_ascii=False), encoding="utf-8")

    reloaded = Config.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert [entry.group_key for entry in reloaded.models.roles.execution] == ["g1"]
    assert reloaded.get_role_model_keys("execution") == ["m_a", "m_b"]
    assert reloaded.get_load_balance_group("g1").max_retry_rounds == 1


def test_quota_pool_key_only_serialized_when_declared() -> None:
    payload = _config_payload()
    payload["models"]["catalog"][0]["quotaPoolKey"] = "gw_shared_60rpm"
    cfg = Config.model_validate(payload)

    assert cfg.get_managed_model("m_a").quota_pool_key == "gw_shared_60rpm"
    catalog_payload, _roles = _managed_models_payload(cfg)
    assert catalog_payload[0].get("quotaPoolKey") == "gw_shared_60rpm"
    assert "quotaPoolKey" not in catalog_payload[1]

    # 空白值等价于未声明，不留 null 字段。
    blank = Config.model_validate(
        {**payload, "models": {**payload["models"], "catalog": [{**payload["models"]["catalog"][0], "quotaPoolKey": "  "}, *payload["models"]["catalog"][1:]]}}
    )
    assert blank.get_managed_model("m_a").quota_pool_key is None
