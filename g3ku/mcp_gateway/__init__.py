"""g3ku MCP gateway — expose a running g3ku runtime to external AI agents.

开箱即用对接面 #2（契约文档：docs/architecture/agent-gateway.md）：
``g3ku mcp serve`` 起一个 stdio MCP 代理进程，经 HTTP+SSE 消费 External
Agent API（``/api/v1``），把对话封装成六个 MCP 工具（g3ku_chat 等），
Claude Code / Cursor 等 MCP 客户端一条命令即可接入。

``server`` 模块（依赖 mcp SDK）由 CLI 命令惰性导入，保持 ``g3ku --help``
启动轻量；本包顶层只 re-export 客户端。
"""

from __future__ import annotations

from g3ku.mcp_gateway.client import DEFAULT_BASE_URL, G3kuMcpClient

__all__ = ["DEFAULT_BASE_URL", "G3kuMcpClient"]
