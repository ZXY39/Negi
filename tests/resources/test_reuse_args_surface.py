"""Stage 2：`reuse_args` 只加在「有契约、且有入参」的工具上，两条投影点同判定。

覆盖 `docs/FIX_PLAN_tool_hydration_contract_presence.md` §4 Stage 2 的四条验证：

- fixed builtin 与无参工具**不带** `reuse_args`，其余 surfaced 工具**带**；
- `reuse_args` 不进任何工具的 `required`；
- 判定函数在两条车道（前门 `_provider_tool_schema` / 节点 `_render_provider_tool_schemas`）
  给出同一个结论，且字段真能穿过 provider 边界；
- 豁免名单与 `main/runtime/stage_budget.py` 里的正本逐名等价（这里不能 import 它，
  见 `g3ku/runtime/tool_visibility.py` 的循环说明——所以用测试钉，而不是靠导入保证）。

语义句只写在**函数级** description：参数级 description 会被
`sanitize_provider_parameters_schema` 逐层剥掉，写在参数上等于在前门车道上不存在。
"""

from __future__ import annotations

from g3ku.agent.tools.base import (
    REUSE_ARGS_MODEL_DESCRIPTION_SENTENCE,
    REUSE_ARGS_PARAMETER_NAME,
    Tool,
    tool_declares_reuse_args,
)
from g3ku.runtime.frontdoor._ceo_runtime_ops import _provider_tool_schema
from g3ku.runtime.tool_visibility import (
    CEO_FIXED_BUILTIN_TOOL_NAMES,
    CEO_NEVER_CONTRACT_TOOL_NAMES,
    NODE_FIXED_BUILTIN_TOOL_NAMES,
    NODE_NEVER_CONTRACT_TOOL_NAMES,
    NON_HYDRATABLE_TOOL_NAMES,
)
from main.runtime import internal_tools
from main.runtime.react_loop import ReActToolLoop
from main.runtime.tool_names import SILENT_TOOL_NAME


async def _callback(*_args: object, **_kwargs: object) -> dict:
    return {}


class _FixtureTool(Tool):
    def __init__(self, name: str, *, parameters: dict | None = None) -> None:
        self._name = name
        self._parameters = parameters or {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} fixture"

    @property
    def parameters(self) -> dict:
        return self._parameters

    async def execute(self, **kwargs: object) -> str:
        return ""


def _surface(tool: Tool) -> dict:
    schema = tool.to_model_schema()
    return schema.get("function") or {}


# ---------------------------------------------------------------------------
# 判定面：谁带字段
# ---------------------------------------------------------------------------


def test_authorized_contract_tool_gets_the_field() -> None:
    body = _surface(_FixtureTool("filesystem_edit"))
    properties = body["parameters"]["properties"]
    assert REUSE_ARGS_PARAMETER_NAME in properties
    assert properties[REUSE_ARGS_PARAMETER_NAME]["type"] == "boolean"
    assert REUSE_ARGS_MODEL_DESCRIPTION_SENTENCE in body["description"]


def test_fixed_builtins_and_argument_less_tools_do_not_get_the_field() -> None:
    for name in (*CEO_FIXED_BUILTIN_TOOL_NAMES, *NODE_FIXED_BUILTIN_TOOL_NAMES):
        tool = _FixtureTool(name)
        assert tool_declares_reuse_args(tool) is False, name
        assert REUSE_ARGS_PARAMETER_NAME not in _surface(tool)["parameters"]["properties"], name

    empty_args = _FixtureTool(
        "task_stats_probe",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    assert tool_declares_reuse_args(empty_args) is False
    assert REUSE_ARGS_PARAMETER_NAME not in _surface(empty_args)["parameters"]["properties"]
    # 无参工具连函数级那句也不该印：它没有可复用的入参，那句话是纯噪声。
    assert REUSE_ARGS_MODEL_DESCRIPTION_SENTENCE not in _surface(empty_args)["description"]


def test_field_never_enters_required() -> None:
    body = _surface(_FixtureTool("filesystem_write"))
    assert REUSE_ARGS_PARAMETER_NAME not in (body["parameters"].get("required") or [])
    # 原有必填不动。
    assert body["parameters"]["required"] == ["path"]


def test_injection_is_idempotent_and_keeps_existing_properties() -> None:
    tool = _FixtureTool(
        "content_open",
        parameters={
            "type": "object",
            "properties": {REUSE_ARGS_PARAMETER_NAME: {"type": "string"}, "ref": {"type": "string"}},
            "required": ["ref"],
        },
    )
    properties = _surface(tool)["parameters"]["properties"]
    # 已经声明了同名参数的工具以它自己的声明为准，不覆盖、不重复注入。
    assert properties[REUSE_ARGS_PARAMETER_NAME] == {"type": "string"}
    assert "ref" in properties


# ---------------------------------------------------------------------------
# 两条投影点同判定，且字段真穿过 provider 边界
# ---------------------------------------------------------------------------


def test_both_provider_surfaces_agree_on_the_field() -> None:
    tool = _FixtureTool("filesystem_edit")
    frontdoor_schema = _provider_tool_schema(tool)
    node_schemas = ReActToolLoop._render_provider_tool_schemas(
        provider_tool_names=["filesystem_edit"],
        current_tools={"filesystem_edit": tool},
        tool_schema_selection=None,
    )
    assert len(node_schemas) == 1
    frontdoor_properties = frontdoor_schema["function"]["parameters"]["properties"]
    node_properties = node_schemas[0]["function"]["parameters"]["properties"]
    assert REUSE_ARGS_PARAMETER_NAME in frontdoor_properties
    assert REUSE_ARGS_PARAMETER_NAME in node_properties
    assert frontdoor_properties[REUSE_ARGS_PARAMETER_NAME] == node_properties[REUSE_ARGS_PARAMETER_NAME]
    # 函数级 description 两句都在：前门那道 sanitize 只剥参数级的 description。
    assert frontdoor_schema["function"]["description"] == node_schemas[0]["function"]["description"]
    assert tool_declares_reuse_args(tool) is True


def test_exempt_names_do_not_get_the_field_on_either_surface() -> None:
    tool = _FixtureTool("load_tool_context")
    frontdoor_properties = _provider_tool_schema(tool)["function"]["parameters"]["properties"]
    node_schemas = ReActToolLoop._render_provider_tool_schemas(
        provider_tool_names=["load_tool_context"],
        current_tools={"load_tool_context": tool},
        tool_schema_selection=None,
    )
    assert REUSE_ARGS_PARAMETER_NAME not in frontdoor_properties
    assert REUSE_ARGS_PARAMETER_NAME not in node_schemas[0]["function"]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# 豁免名单与正本等价（tool_visibility 与 react_loop 不能互相 import：真循环）
# ---------------------------------------------------------------------------


def test_non_hydratable_set_matches_the_authoritative_constants() -> None:
    fixed_builtin_names = (*CEO_FIXED_BUILTIN_TOOL_NAMES, *NODE_FIXED_BUILTIN_TOOL_NAMES)
    for name in (*fixed_builtin_names, SILENT_TOOL_NAME, "load_tool_context", "load_skill_context"):
        assert name in NON_HYDRATABLE_TOOL_NAMES, name
    for name in (*CEO_FIXED_BUILTIN_TOOL_NAMES, *NODE_FIXED_BUILTIN_TOOL_NAMES):
        assert name in NON_HYDRATABLE_TOOL_NAMES, name


def test_every_loop_injected_control_tool_is_exempt() -> None:
    """控制信号工具由循环注入、没有 toolskill，`reuse_args` 对它们是一条永不兑现的承诺。

    正本取 `main/runtime/internal_tools.py` 里的类，逐个实例化读 `.name`：新增一颗控制
    信号而忘了进豁免名单，就在这里红，而不是悄悄在 wire 上多印一个字段。
    """
    names = {
        str(tool.name or '').strip()
        for tool in (
            internal_tools.SilentTool(),
            internal_tools.SpawnChildNodesTool(_callback),
            internal_tools.SubmitFinalResultTool(_callback, node_kind='execution'),
        )
    }
    assert '' not in names
    payload_parameters = {
        "type": "object",
        "properties": {"payload": {"type": "string"}},
        "required": ["payload"],
    }
    for name in sorted(names):
        assert name in NON_HYDRATABLE_TOOL_NAMES, name
        assert name in NODE_NEVER_CONTRACT_TOOL_NAMES, name
        # 带真实入参也不该带字段：投递路径对它们一律 reject，字段就是一个永不兑现的承诺。
        assert tool_declares_reuse_args(_FixtureTool(name, parameters=payload_parameters)) is False, name


def test_node_lane_exemption_does_not_reuse_the_ceo_names() -> None:
    """前门的常驻内置在节点道是带 toolskill 的资源工具，不能跟着一起豁免。

    `memory_note` / `task_summary` 这类名字若在节点道被当成"永不带契约"，冲动路径就不会
    给它们送契约；Stage 5 的在场闸门一上，这批工具会在节点上成批失能（D13）。
    """
    for name in ("memory_note", "task_summary", "task_progress", "create_async_task"):
        assert name in CEO_FIXED_BUILTIN_TOOL_NAMES, name
        assert name not in NODE_NEVER_CONTRACT_TOOL_NAMES, name
        assert name in CEO_NEVER_CONTRACT_TOOL_NAMES, name


def test_universal_timeout_surface_is_untouched() -> None:
    """`timeout_seconds` 是既有注入，新字段不得把它挤掉或改名。"""
    properties = _surface(_FixtureTool("filesystem_edit"))["parameters"]["properties"]
    assert "timeout_seconds" in properties
    assert "path" in properties
