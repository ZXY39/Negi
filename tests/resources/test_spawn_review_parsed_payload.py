"""派生审查车道的候选载荷必须是「解析后的节点结构」。

事故 `task:9b89e2cfc6a5`：评审模型收到的候选把 `acceptance_prompt` /
`requires_acceptance` 与 `goal` / `prompt` 平铺在同一个对象里，于是它把
`requires_acceptance=true` 读成"验收内嵌在生成节点里、等于自产自销"并拦截，
还反过来建议父节点把生成节点和它的验收节点排进同一批——那正是评审规则第 5 条
禁止的形态。父节点服从建议后直接删掉了验收职责，该岗位失去独立验收。

修复口径：载荷按 `_ensure_spawn_acceptance_node` 的真实物化结果展开成
`runtime_nodes`，一个元素 = 运行时创建的一个独立节点；"验收是否独立"变成
数数组长度，而不是猜字段摆放位置。本文件锁定这个形状与它的推断来源。
"""

from __future__ import annotations

from main.models import ExecutionPolicyState, SpawnChildSpec
from main.runtime.node_runner import (
    _SPAWN_ACCEPTANCE_GOAL_PREFIX,
    NodeRunner,
    _spawn_acceptance_goal,
    _spec_requires_acceptance,
)


def _spec(**kwargs: object) -> SpawnChildSpec:
    return SpawnChildSpec(
        goal="生成岗位2三件套",
        prompt="child prompt",
        execution_policy=ExecutionPolicyState(),
        **kwargs,  # type: ignore[arg-type]
    )


def _payload(spec: SpawnChildSpec, index: int = 0) -> dict[str, object]:
    return NodeRunner._spawn_review_requested_spec_payload(index=index, spec=spec)


def test_acceptance_candidate_expands_into_two_independent_nodes() -> None:
    payload = _payload(_spec(requires_acceptance=True, acceptance_prompt="独立核磁盘与 PNG"))

    nodes = payload["runtime_nodes"]
    assert isinstance(nodes, list)
    assert [node["node_kind"] for node in nodes] == ["execution", "acceptance"]
    acceptance = nodes[1]
    assert acceptance["goal"] == f"{_SPAWN_ACCEPTANCE_GOAL_PREFIX}生成岗位2三件套"
    assert acceptance["acceptance_prompt"] == "独立核磁盘与 PNG"
    assert "execution 节点" in str(acceptance["parent_node"])
    assert "终态" in str(acceptance["activation"])
    # 验收标准只存在一份，且不再摆在生成节点对象里。
    assert "acceptance_prompt" not in payload
    assert "acceptance_prompt" not in nodes[0]


def test_missing_flag_infers_acceptance_from_prompt_like_the_runtime_does() -> None:
    spec = _spec(acceptance_prompt="检查产物落盘")

    assert _spec_requires_acceptance(spec) is True
    assert [node["node_kind"] for node in _payload(spec)["runtime_nodes"]] == [  # type: ignore[index]
        "execution",
        "acceptance",
    ]


def test_explicit_false_creates_no_acceptance_node() -> None:
    payload = _payload(_spec(requires_acceptance=False, acceptance_prompt="检查产物落盘"))

    assert payload["requires_acceptance"] is False
    assert payload["runtime_nodes"] == [{"node_kind": "execution"}]


def test_top_level_keys_the_projection_reads_do_not_move() -> None:
    """节点详情的「原始请求」只读顶层 goal 与 execution_policy.mode。"""
    payload = _payload(_spec(requires_acceptance=True, acceptance_prompt="crit"), index=3)

    assert payload["index"] == 3
    assert payload["goal"] == "生成岗位2三件套"
    assert payload["prompt"] == "child prompt"
    assert payload["execution_policy"]["mode"] == "focus"


def test_acceptance_goal_helper_is_the_single_prefix_definition() -> None:
    assert _spawn_acceptance_goal("g") == "accept:g"
    assert _spawn_acceptance_goal("") == _SPAWN_ACCEPTANCE_GOAL_PREFIX
