from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.model_config import ModelConfigTool
from g3ku.config.model_manager import ModelManager
from g3ku.llm_config.enums import ProbeStatus
from g3ku.llm_config.service import ConfigService


def _write_runtime_config(workspace: Path) -> None:
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku" / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "workspace": ".",
                        "runtime": "langgraph",
                        "maxTokens": 1,
                        "temperature": 0.1,
                        "maxToolIterations": 1,
                        "memoryWindow": 1,
                        "reasoningEffort": "low",
                    },
                    "roleIterations": {"ceo": 40, "execution": 16, "inspection": 16},
                    "multiAgent": {"orchestratorModelKey": None},
                },
                "models": {
                    "catalog": [
                        {
                            "key": "m1",
                            "name": "M One",
                            "providerModel": "openai:gpt-4.1",
                            "apiKey": "demo-key",
                            "apiBase": None,
                            "extraHeaders": None,
                            "enabled": True,
                            "maxTokens": 1024,
                            "temperature": 0.1,
                            "reasoningEffort": "low",
                            "retryOn": ["network", "429"],
                            "contextWindowTokens": 128000,
                            "imageMultimodalEnabled": True,
                            "description": "",
                        },
                        {
                            "key": "m2",
                            "name": "M Two",
                            "providerModel": "openai:gpt-4.1-mini",
                            "apiKey": "demo-key-2",
                            "apiBase": None,
                            "extraHeaders": None,
                            "enabled": True,
                            "maxTokens": 1024,
                            "temperature": 0.1,
                            "reasoningEffort": "low",
                            "retryOn": ["network", "429"],
                            "contextWindowTokens": 200000,
                            "imageMultimodalEnabled": False,
                            "description": "",
                        },
                    ],
                    "roles": {
                        "ceo": ["m1", "m2"],
                        # execution 存成对象数组：这是网页保存含组链时的落盘形状，工具的链写必须能保住它。
                        "execution": [
                            {"type": "load_balance", "groupKey": "g1"},
                            {"type": "model", "modelKey": "m1"},
                        ],
                        "inspection": ["m1"],
                        "memory": ["m1"],
                    },
                    "loadBalanceGroups": {
                        # 两个成员：删掉其中一个不会撞上"组里删到空要报错"那条规则。
                        "g1": {"enabled": True, "maxRetryRounds": 4, "modelKeys": ["m1", "m2"]},
                    },
                },
                "providers": {"openai": {"apiKey": "", "apiBase": None, "extraHeaders": None}},
                "web": {"host": "127.0.0.1", "port": 1},
                "toolSecrets": {},
                "resources": {
                    "enabled": True,
                    "skillsDir": "skills",
                    "toolsDir": "tools",
                    "manifestName": "resource.yaml",
                    "reload": {
                        "enabled": True,
                        "pollIntervalMs": 1000,
                        "debounceMs": 400,
                        "lazyReloadOnAccess": True,
                        "keepLastGoodVersion": True,
                    },
                    "locks": {"lockDir": ".g3ku/resource-locks", "logicalDeleteGuard": True, "windowsFsLock": True},
                    "statePath": ".g3ku/resources.state.json",
                },
                "mainRuntime": {
                    "enabled": True,
                    "storePath": ".g3ku/main-runtime/runtime.sqlite3",
                    "filesBaseDir": ".g3ku/main-runtime/tasks",
                    "artifactDir": ".g3ku/main-runtime/artifacts",
                    "governanceStorePath": ".g3ku/main-runtime/governance.sqlite3",
                    "defaultMaxDepth": 1,
                    "hardMaxDepth": 4,
                    "nodeDispatchConcurrency": {"execution": 8, "inspection": 4},
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(root)
    monkeypatch.chdir(root)
    # 工具每次调用都重新 ModelManager.load()，所以要钉在类上而不是某个实例上。
    monkeypatch.setattr(
        ConfigService,
        "probe_draft",
        lambda self, draft: SimpleNamespace(success=True, status=ProbeStatus.SUCCESS, message="ok"),
    )
    return root


def _saved_config(workspace: Path) -> dict:
    return json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))


async def _call(**payload) -> object:
    return await ModelConfigTool().execute(**payload)


async def _prime_models() -> None:
    """补上展示名与收图标记：fixture 的目录项没有 `llmConfigId`，loader 按 legacy 补默认值。"""
    await _call(action="update_model", key="m1", name="M One", image_multimodal_enabled=True)
    await _call(action="update_model", key="m2", name="M Two", image_multimodal_enabled=True)


async def test_update_model_without_provider_model_keeps_secret(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺省的键必须当作"不动这一格"，而不是 None 传下去把 api_key 抹成空。"""
    calls: list[dict] = []
    real_update = ModelManager.update_model

    def _spy(self, **kwargs):  # noqa: ANN001, ANN003
        calls.append(kwargs)
        return real_update(self, **kwargs)

    monkeypatch.setattr(ModelManager, "update_model", _spy)

    result = await _call(action="update_model", key="m1", description="改一下描述")

    assert "Invalid provider_model" not in str(result)
    assert calls and "provider_model" not in calls[0] and "api_key" not in calls[0]
    assert result["description"] == "改一下描述"
    assert result["api_key"]
    assert _saved_config(workspace)["models"]["catalog"][0]["description"] == "改一下描述"


async def test_update_model_writes_window_and_timeout(workspace: Path) -> None:
    result = await _call(
        action="update_model",
        key="m1",
        context_window_tokens="262144",
        request_timeout_seconds=33.5,
        quota_pool_key="pool-a",
        name="M One Renamed",
    )

    assert result["context_window_tokens"] == 262144
    assert result["request_timeout_seconds"] == 33.5
    assert result["quota_pool_key"] == "pool-a"
    assert result["name"] == "M One Renamed"
    reloaded = ModelManager.load().get_model("m1")
    assert reloaded["context_window_tokens"] == 262144
    assert _saved_config(workspace)["models"]["catalog"][0]["contextWindowTokens"] == 262144


async def test_add_model_requires_readable_window_error(workspace: Path) -> None:
    with pytest.raises(ValueError, match="context_window_tokens"):
        await _call(action="add_model", key="m3", provider_model="openai:gpt-4.1", api_key="k3", api_base="")

    created = await _call(
        action="add_model",
        key="m3",
        provider_model="openai:gpt-4.1",
        api_key="k3",
        api_base="",
        context_window_tokens=131072,
        image_multimodal_enabled=True,
        request_timeout_seconds=12,
        name="M Three",
    )
    assert created["context_window_tokens"] == 131072
    assert created["image_multimodal_enabled"] is True
    assert created["request_timeout_seconds"] == 12


async def test_set_scope_chain_with_route_entries_keeps_group_slot(workspace: Path) -> None:
    result = await _call(
        action="set_scope_chain",
        scope="execution",
        route_entries=[
            {"type": "load_balance", "group_key": "g1"},
            {"type": "model", "model_key": "m2"},
        ],
    )

    assert [entry["type"] for entry in result["route_entries"]] == ["load_balance", "model"]
    saved = json.dumps(_saved_config(workspace)["models"]["roles"]["execution"])
    assert "g1" in saved and "m2" in saved


async def test_set_scope_chain_limits_only_leaves_chain_untouched(workspace: Path) -> None:
    result = await _call(action="set_scope_chain", scope="execution", max_iterations=22)

    assert result["max_iterations"] == 22
    assert [entry["type"] for entry in result["route_entries"]] == ["load_balance", "model"]
    assert _saved_config(workspace)["agents"]["roleIterations"]["execution"] == 22


async def test_ceo_scope_rejects_group_entry(workspace: Path) -> None:
    with pytest.raises(ValueError, match="负载均衡组当前仅支持"):
        await _call(
            action="set_scope_chain",
            scope="ceo",
            route_entries=[{"type": "load_balance", "group_key": "g1"}],
        )


async def test_set_scope_chain_rejects_unknown_group(workspace: Path) -> None:
    with pytest.raises(ValueError, match="Unknown load balance group"):
        await _call(
            action="set_scope_chain",
            scope="execution",
            route_entries=[{"type": "load_balance", "group_key": "nope"}],
        )


async def test_get_scope_chain_reports_ordered_entries_and_limits(workspace: Path) -> None:
    await _prime_models()
    result = await _call(action="get_scope_chain", scope="execution")

    assert result["scope"] == "execution"
    assert [entry["type"] for entry in result["route_entries"]] == ["load_balance", "model"]
    # 候选展开视图与有序形状必须同时给出：只有前者时看不出"这一位是组"。
    assert result["model_keys"] == ["m1", "m2"]
    assert result["max_iterations"] == 16
    assert result["chain_image_multimodal_enabled"] is True
    assert result["image_blockers"] == []


async def test_get_scope_chain_alias_and_limits_only_scope(workspace: Path) -> None:
    result = await _call(action="get_scope_chain", scope="checker")

    assert result["scope"] == "inspection"
    assert [entry["model_key"] for entry in result["route_entries"]] == ["m1"]


async def test_list_load_balance_groups_reports_used_by_scopes(workspace: Path) -> None:
    result = await _call(action="list_load_balance_groups")

    item = next(row for row in result["items"] if row["group_key"] == "g1")
    assert item["model_keys"] == ["m1", "m2"]
    assert item["max_retry_rounds"] == 4
    assert item["used_by_scopes"] == ["execution"]


async def test_rename_model_rewrites_chain_and_group_member(workspace: Path) -> None:
    await _call(action="rename_model", key="m2", new_key="m2x")

    saved = _saved_config(workspace)
    assert [str(item) if not isinstance(item, dict) else (item.get("modelKey") or item.get("model_key")) for item in saved["models"]["roles"]["ceo"]] == ["m1", "m2x"]
    group = saved["models"]["loadBalanceGroups"]["g1"]
    members = group.get("modelKeys") or group.get("model_keys")
    assert "m2x" in members and "m2" not in members


async def test_delete_model_reports_removed_from_scopes(workspace: Path) -> None:
    result = await _call(action="delete_model", key="m2")

    # execution 也算：m2 是经组 g1 被那条链用上的，删除同样让该链少一个候选。
    assert sorted(result["removed_from_scopes"]) == ["ceo", "execution"]
    saved = _saved_config(workspace)
    assert [item.get("key") for item in saved["models"]["catalog"]] == ["m1"]
    assert saved["models"]["roles"]["ceo"] == ["m1"]


async def test_image_multimodal_write_echoes_affected_chains(workspace: Path) -> None:
    await _prime_models()
    assert (await _call(action="get_scope_chain", scope="ceo"))["chain_image_multimodal_enabled"] is True

    result = await _call(action="update_model", key="m1", image_multimodal_enabled=False)

    rows = {row["scope"]: row for row in result["image_capability_by_chain"]}
    # m1 在四条链上都在用，所以一条都不能漏报；blockers 用展示名点名拦路成员。
    assert set(rows) == {"ceo", "execution", "inspection", "memory"}
    assert rows["ceo"]["chain_image_multimodal_enabled"] is False
    assert set(rows["ceo"]["image_blockers"]) == {"M One"}
    assert rows["execution"]["chain_image_multimodal_enabled"] is False
    catalog = {item["key"]: item for item in _saved_config(workspace)["models"]["catalog"]}
    assert catalog["m1"]["imageMultimodalEnabled"] is False
    assert catalog["m2"]["imageMultimodalEnabled"] is True


async def test_image_multimodal_chain_decision_is_order_independent(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保守交集与链上顺序无关，解析不到的成员跳过而不是否决整条链。"""
    import main.runtime.react_loop as react_loop_module
    from g3ku.config.loader import load_config
    from g3ku.config.model_manager import image_multimodal_chain_decision
    from main.runtime.react_loop import ReActToolLoop

    await _prime_models()
    await _call(action="update_model", key="m2", image_multimodal_enabled=False)
    cfg = load_config()
    monkeypatch.setattr(react_loop_module, "get_runtime_config", lambda **kwargs: (cfg, 1, True))

    mixed = ["m1", "m2", "ghost-key"]
    reversed_mixed = list(reversed(mixed))

    assert image_multimodal_chain_decision(cfg, mixed) == (False, ["M Two"])
    assert image_multimodal_chain_decision(cfg, reversed_mixed) == (False, ["M Two"])
    # 正样本对照：全收图的链必须判 True，否则上面两条 False 可能只是"什么都没解析到"。
    assert image_multimodal_chain_decision(cfg, ["m1"]) == (True, [])

    assert ReActToolLoop._image_multimodal_enabled_for_model_refs(mixed) is False
    assert ReActToolLoop._image_multimodal_enabled_for_model_refs(reversed_mixed) is False
    assert ReActToolLoop._image_multimodal_enabled_for_model_refs(["m1"]) is True


async def test_set_scope_chain_treats_null_siblings_as_omitted(workspace: Path) -> None:
    """模型会按参数表把没用到的字段填成 null；那不能算"两种链形状同给"。

    现网形状：`{scope, model_keys, route_entries: null}` 报
    `model_keys and route_entries cannot be provided together`，三次链写全红。
    """
    result = await _call(
        action="set_scope_chain",
        scope="inspection",
        model_keys=["m1"],
        route_entries=None,
        max_iterations=None,
        max_concurrency=None,
    )
    assert [entry["model_key"] for entry in result["route_entries"]] == ["m1"]

    result = await _call(
        action="set_scope_chain",
        scope="execution",
        route_entries=[{"type": "load_balance", "group_key": "g1"}],
        model_keys=None,
    )
    assert [entry["type"] for entry in result["route_entries"]] == ["load_balance"]


async def test_set_scope_chain_without_any_payload_is_readable(workspace: Path) -> None:
    result = await _call(action="set_scope_chain", scope="execution", model_keys=None, route_entries=None)
    assert str(result).startswith("Error: set_scope_chain needs")


async def test_update_model_with_all_null_siblings_changes_only_one_field(workspace: Path) -> None:
    """02:58:14 的现网形状：只交 effort 与收图标记，其余字段全为 null。"""
    result = await _call(
        action="update_model",
        key="m1",
        reasoning_effort="high",
        image_multimodal_enabled=True,
        provider_model=None,
        api_key=None,
        api_base=None,
        extra_headers=None,
        max_tokens=None,
        temperature=None,
        context_window_tokens=None,
        request_timeout_seconds=None,
        retry_on=None,
        retry_count=None,
        description=None,
        name=None,
        quota_pool_key=None,
    )

    assert result["reasoning_effort"] == "high"
    assert result["image_multimodal_enabled"] is True
    # 缺席的格子必须原样留着，而不是被 None 抹平。
    assert result["context_window_tokens"] == 128000
    assert result["provider_model"] == "openai:gpt-4.1"
    assert result["max_tokens"] == 1024


async def test_add_model_null_or_missing_window_reports_actionably(workspace: Path) -> None:
    with pytest.raises(ValueError, match="required for add_model"):
        await _call(
            action="add_model",
            key="m9",
            provider_model="openai:gpt-4.1",
            api_key="k9",
            api_base="",
            context_window_tokens=None,
            enabled=None,
        )

    created = await _call(
        action="add_model",
        key="m9",
        provider_model="openai:gpt-4.1",
        api_key="k9",
        api_base="",
        context_window_tokens=131072,
        enabled=None,
        image_multimodal_enabled=None,
        name=None,
    )
    # `enabled: null` 不能把新建的模型落成禁用。
    assert created["enabled"] is True


async def test_refresh_failure_still_reports_config_saved(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import g3ku.shells.web as web_shell

    async def _boom(**kwargs):
        raise RuntimeError("overlay locked")

    monkeypatch.setattr(web_shell, "refresh_web_agent_runtime", _boom)

    with pytest.raises(RuntimeError) as excinfo:
        await ModelConfigTool()._refresh_runtime(
            {"__g3ku_runtime": {"loop": SimpleNamespace(main_task_service=None)}}
        )

    assert "overlay locked" in str(excinfo.value)
    assert "config_saved=true" in str(excinfo.value)
    assert "web_refreshed=false" in str(excinfo.value)


def test_resource_yaml_action_enum_matches_tool_schema() -> None:
    import yaml

    manifest = yaml.safe_load((Path("tools/model_config/resource.yaml").read_text(encoding="utf-8")))
    manifest_enum = manifest["parameters"]["properties"]["action"]["enum"]
    schema_enum = ModelConfigTool().parameters["properties"]["action"]["enum"]

    assert list(manifest_enum) == list(schema_enum)


def test_skill_doc_lists_every_action() -> None:
    text = (Path("tools/model_config/toolskills/SKILL.md").read_text(encoding="utf-8"))
    missing = [action for action in ModelConfigTool().parameters["properties"]["action"]["enum"] if f"`{action}`" not in text]

    assert missing == []
