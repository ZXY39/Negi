from __future__ import annotations

from typing import Final

ENTER_WAIT_TOOL_NAME = "enter_wait"
FINAL_RESULT_TOOL_NAME = "submit_final_result"
LIST_CHILD_NODES_TOOL_NAME = "list_child_nodes"
MANAGE_CHILD_NODES_TOOL_NAME = "manage_child_nodes"
READ_CHILD_PROGRESS_TOOL_NAME = "read_child_progress"
SEND_MESSAGE_TOOL_NAME = "send_message"
SPAWN_CHILD_NODES_TOOL_NAME = "spawn_child_nodes"
SILENT_TOOL_NAME = "silent"
VIEW_MESSAGE_HISTORY_TOOL_NAME = "view_message_history"
VIEW_NODE_TREE_TOOL_NAME = "view_node_tree"

# 两条车道共用的常驻内置：读时钟与定时唤醒自己。它们没有 `tools/<name>/resource.yaml`，
# 因此不进治理表、不进 Tool Admin，也不参与契约在场判据（见
# `g3ku/runtime/tool_visibility._NO_CONTRACT_SIGNAL_TOOL_NAMES`）。
TIME_TOOL_NAME = "time"
SCHEDULE_WAKE_TOOL_NAME = "schedule_wake"

# 读一次时钟不携带任何进度信息：连续三次 `time` 既不能看门狗判成"没有新信息的循环"，
# 也不能被跨轮重复熔断打回——否则模型每轮校时就要付一次软拒，而校时正是本次改造要鼓励的
# 行为（替代 `exec Start-Sleep` 当时钟）。`schedule_wake` 不在这一档：逐字相同的重复设闹钟
# 确实是空转，该让守卫看着。
PROGRESS_NEUTRAL_TOOL_NAMES: Final[frozenset[str]] = frozenset({TIME_TOOL_NAME})

# 合成实例的常驻工具：没有 `tools/<name>/resource.yaml`，所以既不在注册表也不在治理表里，
# 实例由两条车道的执行束就地构造。凡是"按名从 registry 取实例"或"把名字恒定点名进声明面"
# 的地方都读这一份名单——漏一处就是"名字可见但调不动"的那个失败形状（见
# `docs/architecture/tool-and-skill-system.md`「fixed builtin tools」的 silent 一条）。
SYNTHETIC_RESIDENT_TOOL_NAMES: Final[tuple[str, ...]] = (
    SILENT_TOOL_NAME,
    TIME_TOOL_NAME,
    SCHEDULE_WAKE_TOOL_NAME,
)

__all__ = [
    "ENTER_WAIT_TOOL_NAME",
    "FINAL_RESULT_TOOL_NAME",
    "LIST_CHILD_NODES_TOOL_NAME",
    "MANAGE_CHILD_NODES_TOOL_NAME",
    "PROGRESS_NEUTRAL_TOOL_NAMES",
    "READ_CHILD_PROGRESS_TOOL_NAME",
    "SCHEDULE_WAKE_TOOL_NAME",
    "SEND_MESSAGE_TOOL_NAME",
    "SILENT_TOOL_NAME",
    "SPAWN_CHILD_NODES_TOOL_NAME",
    "SYNTHETIC_RESIDENT_TOOL_NAMES",
    "TIME_TOOL_NAME",
    "VIEW_MESSAGE_HISTORY_TOOL_NAME",
    "VIEW_NODE_TREE_TOOL_NAME",
]
