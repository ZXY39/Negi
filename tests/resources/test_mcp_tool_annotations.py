"""MCP 工具面注解合同：九个 `negi_*` 工具每颗都自带四个 hint，不留缺省。

判据轴只有两条，都是客户端要做的决定：
`readOnlyHint` / `destructiveHint` 决定"这颗要不要向用户弹确认"，
`idempotentHint` 决定"客户端能不能自动重发"，
`openWorldHint` 决定"这次调用会不会驱动外部执行"。

为什么必须逐颗写满：规范里这四个 hint 的缺省是
readOnly=false / **destructive=true** / idempotent=false / **openWorld=true**，
所以整块不写注解等于让每个 MCP 客户端把发一条消息当成销毁性动作。
`tools/list` 走真 in-memory MCP 会话取，避免只验到注册对象而漏掉序列化那一层。
契约正文归 docs/architecture/agent-gateway.md「MCP stdio 网关契约」。
"""

from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from g3ku.mcp_gateway.server import build_mcp_server

# tool name -> (readOnlyHint, destructiveHint, idempotentHint, openWorldHint)
EXPECTED_HINTS: dict[str, tuple[bool, bool, bool, bool]] = {
    "negi_chat": (False, False, False, True),
    "negi_get_reply": (False, False, False, False),
    "negi_get_turn": (True, False, True, False),
    "negi_wait_wake": (False, False, False, False),
    "negi_session_status": (True, False, True, False),
    "negi_pause": (False, False, True, False),
    "negi_cancel": (False, True, True, False),
    "negi_list_conversations": (True, False, True, False),
    "negi_delete_conversation": (False, True, True, False),
}


async def _annotated_tools() -> dict[str, tuple[bool, bool, bool, bool]]:
    server = build_mcp_server(None)  # list_tools never touches the HTTP client
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        listed = await session.list_tools()
    return {tool.name: tool.annotations for tool in listed.tools}


@pytest.mark.asyncio
async def test_every_tool_ships_all_four_hints_instead_of_spec_defaults():
    annotations = await _annotated_tools()

    assert set(annotations) == set(EXPECTED_HINTS), sorted(annotations)
    # 缺省即 destructive=true：整块省略注解会把只读读数也画成销毁性工具。
    missing = [name for name, hint in annotations.items() if hint is None]
    assert not missing, f"tools without annotations: {missing}"

    bare = {
        name: (hint.readOnlyHint, hint.destructiveHint, hint.idempotentHint, hint.openWorldHint)
        for name, hint in annotations.items()
    }
    assert bare == EXPECTED_HINTS, {
        name: (bare[name], EXPECTED_HINTS[name]) for name in EXPECTED_HINTS if bare[name] != EXPECTED_HINTS[name]
    }


@pytest.mark.asyncio
async def test_only_the_tools_that_destroy_data_are_marked_destructive():
    annotations = await _annotated_tools()
    destructive = {name for name, hint in annotations.items() if hint.destructiveHint}

    assert destructive == {"negi_cancel", "negi_delete_conversation"}, sorted(destructive)
    # 规范规定 readOnlyHint=true 时 destructiveHint 必须为 false。
    for name, hint in annotations.items():
        if hint.readOnlyHint:
            assert hint.destructiveHint is False, name
    # 只有发消息那颗会驱动外部执行（派出的任务会改仓库、跑命令、发渠道）。
    open_world = {name for name, hint in annotations.items() if hint.openWorldHint}
    assert open_world == {"negi_chat"}, sorted(open_world)
