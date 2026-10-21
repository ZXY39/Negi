"""派发时预置契约（declared seed contracts）的常量与纯判据。

一句话：派发方可选地声明几个工具名，运行时把它们的契约块写进目标节点的 bootstrap，
使这些工具首跳即可直接执行，省掉「交契约 + 暂存入参」那一跳。

本模块只做**不依赖服务层**的两件事：给声明名字定丢弃原因（§优先级），以及按体积上界
裁剪。载荷取数、渲染、写观测都在 `MainRuntimeService`，因为渲染必须走冲动路径同一个函数，
否则指纹字节对不上、预置静默失效。

两个字符上界都是**自设预算**，不是正确性约束：外置闸门 `tool_result_delivery_policy` 只看
`source_kind=tool_result:*`，预置走的是 user 消息，永远不会被外置成信封。超限就削名单，
不为了命中率上调上界。
"""

from __future__ import annotations

from typing import Any

NODE_SEED_CONTRACT_SINGLE_CHAR_LIMIT = 16000
NODE_SEED_CONTRACT_TOTAL_CHAR_LIMIT = 24000

# 优先级从高到低。顺序本身就是契约，两处不可交换：
# - `unknown_tool` 早于角色判据：拼错的名字同时也不在可见集里，反过来会告知派发方「角色不可见」，
#   而它的纠正动作是改名——那它下一步还是填同一个错名字。
# - `repair_required` 早于 `not_visible_for_role`：待修复资源在治理读口里同样不可见
#   （`enabled = tool.enabled and tool.available`），反过来就把「可以提示修复」误报成
#   「这个角色用不了，别再报这个名字」，派发方从此不再报它。
SEED_DROP_REASONS: tuple[str, ...] = (
    'already_always_callable',
    'unknown_tool',
    'repair_required',
    'not_visible_for_role',
    'no_toolskill',
    'render_empty',
    'single_char_limit',
    'total_char_limit',
)


def seed_drop_reason(
    *,
    is_never_contract: bool,
    is_known: bool,
    is_visible: bool,
    is_repair_required: bool,
    has_toolskill_body: bool,
    rendered_block: str,
) -> str:
    """按 `SEED_DROP_REASONS` 的优先级返回第一个命中的原因；全过则返回空串。

    闸门按**目标节点的角色**判，与名单来源无关：同一批工具名在 execution 与 inspection 下
    可见集不同。调用方只负责取数，优先级只在这里判一次——两处各判一遍就会漂移。
    """
    if is_never_contract:
        return 'already_always_callable'
    if not is_known:
        return 'unknown_tool'
    if is_repair_required:
        return 'repair_required'
    if not is_visible:
        return 'not_visible_for_role'
    if not has_toolskill_body:
        return 'no_toolskill'
    if not str(rendered_block or '').strip():
        return 'render_empty'
    return ''


def trim_seed_contracts(
    items: list[dict[str, Any]] | None,
    *,
    single_limit: int = NODE_SEED_CONTRACT_SINGLE_CHAR_LIMIT,
    total_limit: int = NODE_SEED_CONTRACT_TOTAL_CHAR_LIMIT,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """按声明顺序装；返回 (kept, dropped)。

    单份超限记 `single_char_limit` 并**继续看下一个**（一个胖工具不该挤掉后面所有瘦工具），
    装不下的累计量记 `total_char_limit`。
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    used = 0
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get('tool') or '').strip()
        chars = int(item.get('chars') or 0)
        if chars > single_limit:
            dropped.append({'tool': name, 'reason': 'single_char_limit'})
            continue
        if used + chars > total_limit:
            dropped.append({'tool': name, 'reason': 'total_char_limit'})
            continue
        kept.append(item)
        used += chars
    return kept, dropped


__all__ = [
    'NODE_SEED_CONTRACT_SINGLE_CHAR_LIMIT',
    'NODE_SEED_CONTRACT_TOTAL_CHAR_LIMIT',
    'SEED_DROP_REASONS',
    'seed_drop_reason',
    'trim_seed_contracts',
]
