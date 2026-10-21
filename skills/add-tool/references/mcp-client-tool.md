# 把外部 MCP 服务包成工具

适用条件：要接的外部能力**本身是一个 MCP server**（stdio 或 streamable-http），例如桌面自动化服务、厂商 CLI 自带的 MCP 包装。

这条道产出一个普通工具：`tools/<tool_id>/main/tool.py` 里当 **MCP 客户端**，远端本体安装在 `externaltools/<tool_id>/`。**运行时不需要新增任何传输实现**——合同里的 `protocol: mcp` / `mcp.transport: embedded` 说的是模型面形状（`g3ku/resources/loader.py:138-143` 硬校验，写别的直接抛），跟进程间协议无关；官方 Python SDK 已在依赖里（`pyproject.toml:40`，`mcp>=1.26,<2`）。

包成工具之后，候选池、`tools[]` 声明、hydration 提升、阶段记账、超时与取消全部走既有车道，不需要为它开任何特判。

## 一、先定注册粒度（这一步决定后面全部易用性）

远端一般一次暴露十几个到几十个 tool。三种切法：

| 切法 | 代价 |
| --- | --- |
| 远端每个 tool 各包一个 negi 工具 | 全量名字进候选池与钉住的 `tools[]`，前缀体积和"名字选错"的概率一起涨 |
| 一个 handler 全分发（参数 `tool` + `arguments` 字典） | 只占一个名字，但把风险挪到**嵌套 object 参数**——模型会照显示文本把整个 `note:` 块或引号一起传进来，这类形状在本仓已有前科 |
| 高频的 3–5 个各包成扁平工具，冷门走 `exec` | 名字少、参数扁平、冷门不占预算 |

**默认选第三种。** 判据：一回合内会被点名 2 次以上的远端 tool 才值得占一个 negi 名字；其余在 `toolskills/SKILL.md` 里留一条 `exec` + 安装目录驱动脚本的例子即可。`tools/` 只准 `resource.yaml` / `main/` / `toolskills/`，安装物写进 `tools/` 会被直接拒（`g3ku/agent/tools/filesystem_mutation.py:492`），注册文件写完即触发资源刷新、无需重启（同文件 `:532`）。

## 二、会话生命周期：默认 per-call，有数再缓存

本机实测（本地示例 server，`python -m` 级子进程，未含真实服务自身启动成本）：

- per-call（spawn + `initialize` + 1 次 `call_tool`）：中位 **1151 ms**
- 缓存 session（1 次 spawn + 5 次调用）：摊薄 **232 ms/次**，即每多打一次省约 **920 ms**

所以：一回合只打 1–2 次就 per-call；确实要打多次，再升级成进程内缓存 session，并同时补齐三件事——

1. 子进程句柄要持有并挂退出钩子；缓存字典的 key 是 (server command, cwd)，不是工具名。
2. worker 重启 / 模块重新加载后旧 session 一律作废（半截 import 会把带旧状态的模块留在进程里，这条在本仓出过阶段死锁）。
3. 任何等待点都要能响应取消；实测 `stdio_client` 的清理在任务被取消时会带走子进程（见文末测试），但缓存路径上你自己起的那层不在 `async with` 保护内，必须自己收。

重测你自己的服务：同一段脚本换掉 `SERVER_COMMAND` 即可，别把上面这组数当它的数。

## 三、handler 模板

```python
from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from g3ku.agent.tools.base import Tool


def _cancelled(runtime: dict[str, Any]) -> bool:
    token = runtime.get('cancel_token')
    for attr in ('is_cancelled', 'cancelled'):
        flag = getattr(token, attr, None)
        if callable(flag):
            return bool(flag())
        if flag is not None:
            return bool(flag)
    return False


WINDOWS_SPAWN_FLOOR = ('SystemRoot', 'COMSPEC', 'PATHEXT', 'PATH')


def _spawn_env(extra: dict[str, str]) -> dict[str, str]:
    """Windows 兜底三项 + 远端确实需要的变量，点名注入。

    不要传 `env=None`：SDK 给的是它自己的最小默认环境，**不含宿主变量**，远端一读
    自身环境变量就起不来（实测 KeyError）。也不要整份继承 `os.environ`：worker 的
    环境里有模型 API key 这类秘密，交给第三方进程是泄漏面。
    """
    env = {key: os.environ[key] for key in WINDOWS_SPAWN_FLOOR if key in os.environ}
    env.update(extra)
    return env


class ScreenInventoryTool(Tool):
    """一次调用 = 起一次远端、握手、取一个结果、收摊。

    一个类服务多个高频远端 tool，靠构造参数区分，不复制粘贴 N 份。
    """

    # 自己持有子进程，超时由本 handler 兑现，外层不再叠加看门狗
    self_enforced_timeout = True
    # 点鼠标键盘类远端动作不幂等：rerun_safe 保持缺省 False
    rerun_safe = False

    def __init__(
        self,
        *,
        tool_name: str,
        description: str,
        server_command: list[str],
        remote_tool: str,
        server_env: dict[str, str] | None = None,
    ) -> None:
        self._tool_name = str(tool_name)
        self._description = str(description)
        self._server_command = list(server_command)
        self._remote_tool = str(remote_tool)
        self._server_env = dict(server_env or {})

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        # 模型面参数保持扁平：一层、无 object
        return {
            'type': 'object',
            'properties': {'text': {'type': 'string', 'description': '要回显的文本'}},
            'required': ['text'],
        }

    async def execute(self, timeout_seconds: float | None = None, **kwargs: Any) -> str:
        runtime = kwargs.pop('__g3ku_runtime', None) or {}
        arguments = dict(kwargs)  # 模型面扁平参数直接当远端参数；名字不一致时在这里映射
        limit = max(1.0, float(timeout_seconds or 30.0))
        params = StdioServerParameters(
            command=self._server_command[0],
            args=self._server_command[1:],
            env=_spawn_env(self._server_env),
        )
        call = asyncio.ensure_future(self._call(params, limit, runtime, arguments))
        done, _pending = await asyncio.wait({call}, timeout=limit)
        if not done:
            # stdio_client 的清理在取消时会把自己的 BrokenResourceError 包成
            # ExceptionGroup 抛出来，那会把 wait_for 的 TimeoutError 顶掉。
            # 自己收口：取消后把清理异常吃掉，超时仍然是一条普通错误回执。
            call.cancel()
            try:
                await call
            except (asyncio.CancelledError, BaseExceptionGroup):
                pass
            return f'Error: {self.name} 超过 {limit:.0f}s 未返回，远端调用已被放弃'
        return call.result()

    async def _call(
        self,
        params: StdioServerParameters,
        limit: float,
        runtime: dict[str, Any],
        arguments: dict[str, Any],
    ) -> str:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if _cancelled(runtime):
                    raise asyncio.CancelledError
                result = await session.call_tool(
                    self._remote_tool,
                    arguments,
                    read_timeout_seconds=timedelta(seconds=limit),
                )
        if getattr(result, 'isError', False):
            # 远端原文直出，别自己概括成"调用失败"
            return f'Error: {self.name} 远端返回: {result.content[0].text}'
        return str(result.content[0].text)
```

`build(runtime)` 侧把命令拼出来（`runtime.workspace` 由 loader 注入，见 `g3ku/resources/loader.py:117-133`）：

```python
def build(runtime):
    root = runtime.workspace / 'externaltools' / 'screen_mcp'
    command = [str(root / '.venv' / 'Scripts' / 'python.exe'), '-m', 'their_mcp_server']
    return ScreenInventoryTool(
        tool_name='screen_inventory',
        description='读取显示器布局、分辨率与 DPI 缩放。只读，不改变任何窗口状态。',
        server_command=command,
        remote_tool='DisplayInventory',
        server_env={'THEIR_MCP_HOME': str(root)},
    )
```

模板里四处是本仓特有、别处抄不来的：`timeout_seconds` 由运行时注入进模型面（`g3ku/agent/tools/base.py:214-239`，单位是秒，写 `60000` 会被读成 16.6 小时）；`self_enforced_timeout = True` 意味着**外层不再替你兜**，你必须自己 `wait_for`；运行帧从 `kwargs.pop('__g3ku_runtime')` 拿（`g3ku/agent/tools/shell.py:213`），取消标志读 `is_cancelled` / `cancelled`（与 `main/governance/exec_approvals.py:367-379` 同一判据）；子进程环境走 `_spawn_env` 点名注入，理由见那段注释。

## 四、`resource.yaml` 上的差异点

通用清单见 `references/toolskill-checklist.md`，这里只列 MCP 相关的两行：

- `requires.bins` 填**你真正会去 which 的那个入口**（通常是 `externaltools/<tool_id>/.venv/Scripts/python.exe` 的绝对路径或该 venv 的启动器），不要填 `python`——那样即使远端没装也判可用。
- `governance.allowed_roles` 新建时只给 `ceo`，由操作员在 Tool Admin 放大。理由见下一节。

## 五、安全面：这条车道没有 exec 的那些守卫

自写 handler 是**进程内任意 Python**，不经过 `exec` 的命令形态守卫、白名单与审批车道（那套只作用于 `exec`）。MCP 包装还额外带来一个子进程和"远端能操作桌面"的能力，所以：

- `toolskills/SKILL.md` 的 `## 何时使用` 必须写明它会真的动鼠标键盘 / 改窗口，以及怎么进入用户接管态；不要只写"可操作桌面"。
- 只读类（列显示器、列窗口）与写类（点击、输入）**分成不同工具**，别用同一个工具加一个 `dry_run` 参数——治理按工具名授权，按参数授权在这套合同里没有第二道闸。
- 结果文本要有界：远端 UIA 元素树动辄几万字符，直出会把上下文吃掉；模板里只取 `content[0].text`，若远端可能返回大块内容，先截断并在 toolskill 里说明截断规则与取全量的办法。

## 六、实测锚点

`tests/resources/test_mcp_client_tool_recipe.py` 用本地 stdio server 跑上面这段模板的调用序列：握手 → 列工具 → 调用 → 断言取消后子进程不残留（心跳文件 + 进程存活双判）。模板的调用形状改了，这条测试要同步改。
