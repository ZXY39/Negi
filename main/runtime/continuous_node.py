"""连续（待命）节点的共享判据与文案。

单一真值是节点 metadata 的 `continuous` 位：创建时写定，之后不可变。

连续节点没有提交/验收通道（机制层）：`submit_final_result` 不进它们的工具面，
验收节点也不会为它们派生——包括显式要求。结束职责的唯一通道是把结束请求
`send_message` 给上游：子节点发给父节点（由父节点 `manage_child_nodes(terminate)`
结束），根节点发给前门（由 Master 决定是否取消任务）。
"""

from __future__ import annotations

from typing import Any

CONTINUOUS_METADATA_KEY = 'continuous'

# spawn 回执里的软拒绝文案：调用本身成功返回，不建验收节点，也不报错打断
# 父节点的派生流程；原因随回执交回，让父节点知道该子节点不走验收。
CONTINUOUS_ACCEPTANCE_REJECTION_NOTE = (
    '该子节点是连续节点，不允许验收：本次验收请求未创建，不存在验收节点。'
    '它完成工作后直接 `enter_wait` 待命；需要结束它的职责时，'
    '由你调用 `manage_child_nodes(terminate)`（需附 reason）。'
)


def is_continuous_metadata(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get(CONTINUOUS_METADATA_KEY))
