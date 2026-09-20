"""spawn review 车道的外部检验回合语义回归。

该车道故意不走节点 send preflight，也没有 actual-request 工件，历史上有两个静默面：

- 无效响应重发没有次数上限（同文件的消息分发决策车道有 `_DISTRIBUTION_DECISION_MAX_ATTEMPTS`）；
- 车道的正文规模与重试次数不留任何证据，用量只并进父节点聚合，事后无法还原。

本文件锁定：重发封顶后按 fail-closed 默认结果收口，且 `review_attempts` /
`review_request_chars` 在成功与降级两条返回上都落进 spawn_review 载荷。
"""

from __future__ import annotations

from typing import Any

import pytest

import main.runtime.node_runner as node_runner_module
from g3ku.providers.base import LLMResponse
from main.models import ExecutionPolicyState, SpawnChildSpec
from main.runtime.node_runner import NodeRunner


class _RecordingBackend:
    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.calls = 0

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        return self._response


class _ReviewRunnerStub:
    """只绑定被测回合真正用到的方法，消息装配按固定桩替代。"""

    _review_spawn_batch = NodeRunner._review_spawn_batch
    _default_spawn_review_result = NodeRunner._default_spawn_review_result
    _parse_spawn_review_response = NodeRunner._parse_spawn_review_response
    _spawn_review_tool_schema = staticmethod(NodeRunner._spawn_review_tool_schema)
    _spawn_review_repair_message = staticmethod(NodeRunner._spawn_review_repair_message)
    _spawn_review_requested_spec_payload = staticmethod(NodeRunner._spawn_review_requested_spec_payload)

    def __init__(self, backend: _RecordingBackend) -> None:
        self._react_loop = type("RL", (), {"_chat_backend": backend})()
        self._acceptance_model_refs = ["test:review-model"]
        self._execution_model_refs: list[str] = []
        self.usage_records = 0

    def _spawn_review_messages(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": "review prompt"},
            {"role": "user", "content": "review context payload"},
        ]

    def _record_spawn_review_token_usage(self, **_kwargs: Any) -> None:
        self.usage_records += 1


def _specs(count: int) -> list[SpawnChildSpec]:
    policy = ExecutionPolicyState()
    return [
        SpawnChildSpec(goal=f"goal {index}", prompt=f"prompt {index}", execution_policy=policy)
        for index in range(count)
    ]


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_runner_module, "_SPAWN_REVIEW_RETRY_DELAY_SECONDS", 0.0)


async def test_unparseable_reviews_stop_at_cap_and_fail_closed() -> None:
    backend = _RecordingBackend(LLMResponse(content="I am not a decision", attempts=[]))
    runner = _ReviewRunnerStub(backend)
    specs = _specs(3)

    result = await runner._review_spawn_batch(
        task=type("T", (), {"task_id": "task:review-cap"})(),
        parent=type("N", (), {"node_id": "node:parent"})(),
        specs=specs,
        cache_key="call:review-cap",
    )

    assert backend.calls == node_runner_module._SPAWN_REVIEW_MAX_ATTEMPTS
    assert result["allowed_indexes"] == []
    assert len(result["blocked_specs"]) == len(specs)
    assert "unparseable" in result["error_text"]
    assert result["review_outcome"] == "system_failure"
    assert result["review_attempts"] == node_runner_module._SPAWN_REVIEW_MAX_ATTEMPTS
    # 降级路径同样要留下体积证据，否则这条车道仍然不可度量
    assert result["review_request_chars"] == len("review prompt") + len("review context payload")
    assert runner.usage_records == 0


async def test_first_attempt_review_records_attempts_and_request_size() -> None:
    decision = '{"allowed_indexes": [0, 1], "blocked_specs": []}'
    backend = _RecordingBackend(LLMResponse(content=decision, attempts=[]))
    runner = _ReviewRunnerStub(backend)

    result = await runner._review_spawn_batch(
        task=type("T", (), {"task_id": "task:review-ok"})(),
        parent=type("N", (), {"node_id": "node:parent"})(),
        specs=_specs(2),
        cache_key="call:review-ok",
    )

    assert backend.calls == 1
    assert result["allowed_indexes"] == [0, 1]
    assert result["error_text"] == ""
    assert result["review_outcome"] == "verdict"
    assert result["review_attempts"] == 1
    assert result["review_request_chars"] > 0
    assert runner.usage_records == 1


class _ApplyStub:
    """`_apply_spawn_review_results` 是按白名单重建持久载荷的，新增字段必须在这里
    也落一次——否则车道证据只存在于函数返回值里，节点详情与投影永远读不到。
    被拦截结果的构造走真实实现，分型（裁决 vs 车道故障）正是那条路径要锁的行为。"""

    _apply_spawn_review_results = NodeRunner._apply_spawn_review_results
    _spawn_review_blocked_result = NodeRunner._spawn_review_blocked_result

    def __init__(self) -> None:
        self.saved: dict[str, Any] = {}
        self.entry_updates: list[dict[str, Any]] = []

    def _update_spawn_entry(self, **kwargs: Any) -> None:
        self.entry_updates.append(kwargs)

    def _spawn_entry_non_terminal_node(self, _entry: Any) -> str:
        return ""

    def _warn_spawn_entry_review_over_live_node(self, **_kwargs: Any) -> None:
        return None

    def _save_spawn_cache(self, _task_id: str, _node_id: str, _cache_key: str, payload: dict) -> None:
        self.saved.update(payload)


def _apply_stub_review(stub: _ApplyStub, *, outcome: str, specs: list[SpawnChildSpec]) -> Any:
    return stub._apply_spawn_review_results(
        task_id="task:review-persist",
        parent_node_id="node:parent",
        cache_key="call:review-persist",
        cached_payload={"entries": [{}, {}]},
        specs=specs,
        spawn_review={
            "reviewed_at": "2026-09-20T00:00:00",
            "requested_specs": [],
            "allowed_indexes": [0],
            "blocked_specs": [{"index": 1, "reason": "dup" if outcome == "verdict" else "429 storm", "suggestion": "split"}],
            "error_text": "" if outcome == "verdict" else "Error code: 429",
            "review_outcome": outcome,
            "review_attempts": 3,
            "review_request_chars": 4242,
        },
    )


def test_review_forensics_survive_the_persisted_spawn_payload() -> None:
    runner = _ApplyStub()

    allowed = _apply_stub_review(runner, outcome="verdict", specs=_specs(2))

    assert allowed == [0]
    persisted = runner.saved["spawn_review"]
    assert persisted["review_attempts"] == 3
    assert persisted["review_request_chars"] == 4242
    assert persisted["review_outcome"] == "verdict"


def test_system_failure_outcome_is_typed_apart_from_a_review_verdict() -> None:
    runner = _ApplyStub()

    _apply_stub_review(runner, outcome="system_failure", specs=_specs(2))

    blocked = [u for u in runner.entry_updates if u.get("review_decision") == "blocked"]
    assert len(blocked) == 1
    assert blocked[0]["blocked_reason"] == "429 storm"
    assert runner.saved["spawn_review"]["review_outcome"] == "system_failure"
    assert runner.saved["spawn_review"]["error_text"] == "Error code: 429"
    # 故障态不得回落到"未批准该候选"那句兜底文案
    assert "未批准" not in blocked[0]["blocked_reason"]


def test_blocked_result_text_names_the_existing_final_result_call() -> None:
    failure = NodeRunner._spawn_review_blocked_result(
        _specs(1)[0],
        reason="Error code: 429",
        suggestion="",
        system_failure=True,
    )
    verdict = NodeRunner._spawn_review_blocked_result(
        _specs(1)[0],
        reason="与兄弟节点重复",
        suggestion="拆批",
        system_failure=False,
    )

    assert failure.check_result == "派生未审查（系统故障）"
    assert verdict.check_result == "派生已被拦截"
    assert failure.failure_info is not None
    assert failure.failure_info.source == "runtime"
    assert failure.failure_info.delivery_status == "blocked"
    assert "Error code: 429" in failure.failure_info.blocking_reason
    assert verdict.failure_info is None
    # 文案必须指向真实存在的工具与 schema 字段，否则模型会去找一个不存在的动作
    assert "submit_final_result" in failure.node_output
    assert "delivery_status='blocked'" in failure.node_output
    assert "派生未被审查" in failure.node_output
    assert "submit_final_result" not in verdict.node_output
