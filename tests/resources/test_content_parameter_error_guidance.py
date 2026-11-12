"""content 家族的结构化参数错误必须接进既有引导车道（前门 / 节点两条执行道）。

content 家族把参数类失败编码成结构化 `{"ok": false, ...}`，而不是抛出异常；既有引导
车道（`append_parameter_error_guidance`）只认抛出的 `ValueError` / `TypeError`，所以
这类回执进了错误车道、却在引导车道之外。跨层契约只有一条通道：content 层在异常还在
手的地方挂 `parameter_error: true` 布尔，引导由执行道用资源工具实例渲染。
`PermissionError` / `FileNotFoundError` 沿用既有谓词保持原语义，不挂引导。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.content.tool import ContentTool
from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport
from g3ku.runtime.tool_error_guidance import parameter_error_guidance
from g3ku.runtime.tool_result_status import (
    is_error_like_tool_result,
    is_parameter_error_tool_result,
)
from main.runtime.react_loop import ReActToolLoop

TOOL_NAME = "content_open"
PRESENCE = {TOOL_NAME: "tcf:" + "a" * 16}
ERROR_TEXT = "choose either start_char/end_char or line selectors"


def _toolskill_file(tmp_path: Path) -> Path:
    path = tmp_path / "toolskills" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# content_open contract", encoding="utf-8")
    return path


class _JsonResultTool(Tool):
    """Resource 包装层的替身：回执与 ContentTool 的结构化错误同形。"""

    def __init__(self, *, toolskill_path: Path, flagged: bool = True) -> None:
        self._descriptor = SimpleNamespace(toolskills_main_path=toolskill_path, metadata={})
        self._flagged = flagged

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return "content open"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def execute(self, **kwargs):
        _ = kwargs
        payload = {"ok": False, "error": ERROR_TEXT}
        if self._flagged:
            payload["parameter_error"] = True
        return json.dumps(payload, ensure_ascii=False)


class _RaisingTool(Tool):
    def __init__(self, *, toolskill_path: Path, exc: BaseException) -> None:
        self._descriptor = SimpleNamespace(toolskills_main_path=toolskill_path, metadata={})
        self._exc = exc

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return "content open"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def execute(self, **kwargs):
        _ = kwargs
        raise self._exc


class _ContentLaneTool(Tool):
    """真实 ContentTool 的资源包装层替身：透传执行并携带 descriptor。"""

    def __init__(self, handler: ContentTool, *, toolskill_path: Path) -> None:
        self._handler = handler
        self._descriptor = SimpleNamespace(toolskills_main_path=toolskill_path, metadata={})

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return "content open"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def execute(self, **kwargs):
        return await self._handler.execute(action="open", **kwargs)


class _FakeTools:
    def push_runtime_context(self, _context):
        return object()

    def pop_runtime_context(self, _token):
        return None


class _FakeLoop:
    def __init__(self) -> None:
        self.tools = _FakeTools()


class _Harness(CeoFrontDoorSupport):
    def __init__(self, loop) -> None:
        self._loop = loop


def _frontdoor_context() -> dict:
    return {
        "session_key": "web:test",
        "actor_role": "ceo",
        "fs_read_roots": (),
        "tool_contract_presence": dict(PRESENCE),
        "tool_watchdog": {
            "default_timeout_seconds": 600,
            "poll_interval_seconds": 0.01,
            "auto_background_seconds": 0,
        },
    }


def _node_loop() -> ReActToolLoop:
    return ReActToolLoop(
        chat_backend=SimpleNamespace(),
        log_service=SimpleNamespace(),
        max_iterations=2,
    )


def _node_context() -> dict:
    return {
        "task_id": "task-guidance",
        "node_id": "node-guidance",
        "actor_role": "execution",
        "tool_contract_presence": dict(PRESENCE),
        "tool_watchdog": {"default_timeout_seconds": 600, "poll_interval_seconds": 0.01},
    }


async def _run_frontdoor(tool: Tool, *, arguments: dict):
    harness = _Harness(_FakeLoop())
    return await harness._execute_tool_call_with_raw_result(
        tool=tool,
        tool_name=tool.name,
        arguments=arguments,
        runtime_context=_frontdoor_context(),
        on_progress=None,
        tool_call_id="call-1",
    )


async def _run_node(tool: Tool, *, arguments: dict):
    loop = _node_loop()
    return await loop._execute_tool_with_status(
        tools={tool.name: tool},
        tool_name=tool.name,
        arguments=arguments,
        runtime_context=_node_context(),
    )


# --- 引导正样本：两条道各一，且同形 -------------------------------------------


@pytest.mark.asyncio
async def test_full_slot_incident_call_from_the_lane_uses_line_addressing(tmp_path: Path) -> None:
    """事故原样的 9 个槽位从 ContentTool.execute 喂进去：空 ref 走 path 模式、行寻址、raw 视图。"""
    target = tmp_path / "facts.md"
    target.write_text("\n".join(f"line-{index}" for index in range(1, 51)), encoding="utf-8")
    tool = ContentTool(workspace=tmp_path)

    raw = await tool.execute(
        action="open",
        ref="",
        path=str(target),
        view="raw",
        start_line=1,
        end_line=230,
        start_char=1,
        end_char=23_000,
        reuse_args=False,
        timeout_seconds=20,
    )

    payload = json.loads(raw)
    assert payload.get("ok") is True
    assert payload.get("start_line") == 1
    assert (payload.get("excerpt") or "").startswith("1: line-1")
    assert payload.get("selector_conflict") == {
        "used": "line",
        "discarded": {"start_char": 1, "end_char": 23_000},
    }


@pytest.mark.asyncio
async def test_combined_target_conflict_stays_on_its_own_target(tmp_path: Path) -> None:
    target = tmp_path / "facts.md"
    target.write_text("\n".join(f"line-{index}" for index in range(1, 11)), encoding="utf-8")
    tool = ContentTool(workspace=tmp_path)

    raw = await tool.execute(
        action="open",
        ref="path:facts.md",
        path=str(target),
        start_line=1,
        end_line=3,
        start_char=1,
        end_char=20_000,
    )

    payload = json.loads(raw)
    assert payload.get("ok") is True
    assert payload.get("combined") is True
    for name in ("ref", "path"):
        result = payload["targets"][name]
        assert result.get("ok") is True
        assert result.get("selector_conflict", {}).get("used") == "line"
        assert "1: line-1" in (result.get("excerpt") or "")


@pytest.mark.asyncio
async def test_frontdoor_lane_appends_guidance_for_structured_parameter_error(tmp_path: Path) -> None:
    tool = _JsonResultTool(toolskill_path=_toolskill_file(tmp_path))

    raw, rendered, status, *_ = await _run_frontdoor(tool, arguments={"path": "x.md"})

    assert status == "error"
    payload = json.loads(raw)
    assert payload.get("ok") is False
    assert "parameter_error" not in payload
    error_text = str(payload.get("error") or "")
    assert ERROR_TEXT in error_text
    assert f'content_open(path="{tool._descriptor.toolskills_main_path}")' in error_text
    assert "load_tool_context(tool_id=" not in error_text
    assert json.loads(rendered) == payload
    assert is_error_like_tool_result(raw) is True


@pytest.mark.asyncio
async def test_node_lane_appends_guidance_for_structured_parameter_error(tmp_path: Path) -> None:
    tool = _JsonResultTool(toolskill_path=_toolskill_file(tmp_path))

    rendered, status = await _run_node(tool, arguments={"path": "x.md"})

    assert status == "error"
    payload = json.loads(rendered)
    assert payload.get("ok") is False
    assert "parameter_error" not in payload
    error_text = str(payload.get("error") or "")
    assert ERROR_TEXT in error_text
    assert f'content_open(path="{tool._descriptor.toolskills_main_path}")' in error_text


@pytest.mark.asyncio
async def test_both_lanes_render_the_same_guidance_for_the_same_call(tmp_path: Path) -> None:
    tool = _JsonResultTool(toolskill_path=_toolskill_file(tmp_path))
    arguments = {"path": "x.md"}

    _, front_rendered, front_status, *_ = await _run_frontdoor(tool, arguments=dict(arguments))
    node_rendered, node_status = await _run_node(tool, arguments=dict(arguments))

    assert front_status == node_status == "error"
    assert json.loads(front_rendered)["error"] == json.loads(node_rendered)["error"]


@pytest.mark.asyncio
async def test_real_content_tool_parameter_error_carries_guidance_through_the_frontdoor_lane(
    tmp_path: Path,
) -> None:
    """生产侧到消费侧的整条链：ContentTool 挂布尔 → 前门道渲染指针。"""
    directory = tmp_path / "sub"
    directory.mkdir()
    tool = _ContentLaneTool(ContentTool(workspace=tmp_path), toolskill_path=_toolskill_file(tmp_path))

    raw, _rendered, status, *_ = await _run_frontdoor(tool, arguments={"path": str(directory)})

    assert status == "error"
    payload = json.loads(raw)
    assert "path is not a file" in str(payload.get("error") or "")
    assert "content_open(path=" in str(payload.get("error") or "")


# --- 引导负样本：不带字段与参数类以外的异常都不挂 ------------------------------


@pytest.mark.asyncio
async def test_unflagged_structured_error_gets_no_guidance_on_either_lane(tmp_path: Path) -> None:
    tool = _JsonResultTool(toolskill_path=_toolskill_file(tmp_path), flagged=False)

    _, front_rendered, front_status, *_ = await _run_frontdoor(tool, arguments={"path": "x.md"})
    node_rendered, node_status = await _run_node(tool, arguments={"path": "x.md"})

    assert front_status == node_status == "error"
    for text in (front_rendered, node_rendered):
        assert json.loads(text) == {"ok": False, "error": ERROR_TEXT}
        assert "load_tool_context(tool_id=" not in text
        assert "content_open(path=" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [PermissionError("denied"), FileNotFoundError("missing.txt")],
    ids=["permission", "not-found"],
)
async def test_non_parameter_exceptions_keep_their_semantics_on_the_frontdoor_lane(
    tmp_path: Path, exc: BaseException
) -> None:
    tool = _RaisingTool(toolskill_path=_toolskill_file(tmp_path), exc=exc)

    raw, rendered, status, *_ = await _run_frontdoor(tool, arguments={"path": "x.md"})

    assert status == "error"
    assert str(raw).startswith(f"Error executing {TOOL_NAME}: ")
    assert "load_tool_context(tool_id=" not in rendered
    assert "content_open(path=" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [PermissionError("denied"), FileNotFoundError("missing.txt")],
    ids=["permission", "not-found"],
)
async def test_non_parameter_exceptions_are_not_relabelled_on_the_node_lane(
    tmp_path: Path, exc: BaseException
) -> None:
    tool = _RaisingTool(toolskill_path=_toolskill_file(tmp_path), exc=exc)
    loop = _node_loop()

    with pytest.raises(type(exc)):
        await loop._execute_tool_raw(
            tools={tool.name: tool},
            tool_name=tool.name,
            arguments={"path": "x.md"},
            runtime_context=_node_context(),
        )


# --- 判据不变臂 ---------------------------------------------------------------


def test_parameter_error_flag_is_readable_and_keeps_the_error_verdict() -> None:
    flagged = json.dumps({"ok": False, "error": "bad", "parameter_error": True})
    unflagged = json.dumps({"ok": False, "error": "bad"})
    success = json.dumps({"ok": True, "excerpt": "x"})

    assert is_parameter_error_tool_result(flagged) is True
    assert is_parameter_error_tool_result(unflagged) is False
    assert is_parameter_error_tool_result(success) is False
    assert is_parameter_error_tool_result("Error: plain text") is False

    assert is_error_like_tool_result(flagged) is True
    assert is_error_like_tool_result(unflagged) is True
    assert is_error_like_tool_result(success) is False


# --- 指针可达臂 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_present_pointer_names_a_content_open_call_that_succeeds(tmp_path: Path) -> None:
    skill = _toolskill_file(tmp_path)
    tool = _JsonResultTool(toolskill_path=skill)

    guidance = parameter_error_guidance(
        TOOL_NAME,
        tool=tool,
        arguments={"path": "x.md"},
        runtime_context={"tool_contract_presence": dict(PRESENCE)},
    )

    assert f'content_open(path="{skill}")' in guidance
    content_tool = ContentTool(workspace=tmp_path)
    opened = json.loads(await content_tool.execute(action="open", path=str(skill)))
    assert opened.get("ok") is True
