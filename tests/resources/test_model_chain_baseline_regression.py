"""Phase 0 基线：钉死「旧 flat chain + 旧准入」的现状行为。

这些断言描述的是**改造前**的行为，其中多数会在后续 Phase 被有意推翻：

- 准入层按链首预占 model/key permit，并在整次 `chat()` 期间攥着不放（Phase 3 把首
  次选择移到准入层，这条变成「按 balancer 选中的成员预占」）。
- 退避只发生在同模型轮之间，跨模型前进零等待（Phase 3 在组内成员之间补节拍）。
- `retry_count=0/未配置` 落 `DEFAULT_RETRYABLE_MODEL_ROUNDS`，不是 1 轮（Phase 3 的
  group 预算必须显式写值才拿得到 1）。

把它们单独放一个文件，是为了让改动时的 diff 可读：删掉的是哪条契约，一目了然。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import g3ku.providers.fallback as fallback_module
import main.runtime.chat_backend as chat_backend_module
from g3ku.providers.base import LLMResponse
from g3ku.providers.provider_factory import ProviderTarget
from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.node_turn_controller import NodeTurnLease


class _StatusError(RuntimeError):
    """携带结构化 HTTP 状态的异常，模拟 OpenAI SDK 异常（.status_code 可得）。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _AuthFailProvider:
    """401 未命中 retry_on → 单趟换 key，key 用尽后前进到链上下一个模型。"""

    def __init__(self, model_key: str, calls: list[str], on_call=None) -> None:
        self.model_key = model_key
        self.calls = calls
        self.on_call = on_call

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.model_key)
        if self.on_call is not None:
            self.on_call()
        raise _StatusError("HTTP 401: invalid api key", 401)


class _OkProvider:
    def __init__(self, model_key: str, calls: list[str], on_call=None) -> None:
        self.model_key = model_key
        self.calls = calls
        self.on_call = on_call

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.model_key)
        if self.on_call is not None:
            self.on_call()
        return LLMResponse(content="ok", finish_reason="stop")


class _AlwaysRetryableProvider:
    """502 命中 retry_on → 走该模型的退避重试轮预算。"""

    def __init__(self, model_key: str, calls: list[str]) -> None:
        self.model_key = model_key
        self.calls = calls

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.model_key)
        raise RuntimeError("HTTP 502: upstream request failed")


def _target(provider, *, model_key: str, retry_count: int, api_key_count: int = 1) -> ProviderTarget:
    return ProviderTarget(
        provider_ref=str(model_key),
        provider_id="custom",
        model_id=f"{model_key}-model",
        provider=provider,
        retry_on=["network", "429", "502"],
        retry_count=retry_count,
        api_key_count=api_key_count,
        api_key_indexes=list(range(api_key_count)),
    )


def _patch_providers(monkeypatch, providers: dict[str, ProviderTarget]) -> None:
    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return providers[str(model_key)]

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)


def _backend() -> chat_backend_module.ConfigChatBackend:
    return chat_backend_module.ConfigChatBackend(
        config=SimpleNamespace(get_model_runtime_profile=lambda ref: None)
    )


def _make_controller() -> ModelKeyConcurrencyController:
    # 每模型单 key、单 key 上限 1：permit 的占用与释放都能被 model_state 直接观察到。
    return ModelKeyConcurrencyController(
        resolve_model_limits=lambda model_ref: {"key_count": 1, "per_key_limit": 1, "key_indexes": [0]},
    )


def _lease_for(controller: ModelKeyConcurrencyController, model_ref: str) -> NodeTurnLease:
    permit = controller.try_acquire_first_available(model_ref=model_ref)
    assert permit is not None
    return NodeTurnLease(
        lease_id=1,
        task_id="task:baseline",
        node_id="node:baseline",
        model_ref=str(permit.model_ref),
        key_index=int(permit.key_index),
        acquired_at="",
        initial_model_permit=permit,
    )


@pytest.mark.asyncio
async def test_flat_chain_attempts_models_in_configured_order(monkeypatch) -> None:
    """现状：`model_refs` 的列表顺序就是执行/检验节点的 fallback 顺序。"""
    calls: list[str] = []
    providers = {
        "a": _target(_AuthFailProvider("a", calls), model_key="a", retry_count=0),
        "b": _target(_AuthFailProvider("b", calls), model_key="b", retry_count=0),
        "c": _target(_OkProvider("c", calls), model_key="c", retry_count=0),
    }
    _patch_providers(monkeypatch, providers)

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["a", "b", "c"],
    )

    assert response.content == "ok"
    assert calls == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_admission_permit_is_consumed_by_chain_head_attempt(monkeypatch) -> None:
    """现状：准入预占的那颗 permit 正好被链首的第一次 attempt 消费掉，链首失败后随
    attempt 结束归还——所以今天不会出现「虚挂在链首的 running」。Phase 3 依赖的正是
    这个消费口（`use_held_turn_permit`），改成 balancer 选定成员后必须继续走它。"""
    controller = _make_controller()
    lease = _lease_for(controller, "a")

    calls: list[str] = []
    head_running: list[int] = []

    def _observe_head() -> None:
        state = controller.model_state("a")
        head_running.append(sum(int(v or 0) for v in state["running"].values()))

    providers = {
        "a": _target(_AuthFailProvider("a", calls, on_call=_observe_head), model_key="a", retry_count=0),
        "b": _target(_OkProvider("b", calls, on_call=_observe_head), model_key="b", retry_count=0),
    }
    _patch_providers(monkeypatch, providers)

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["a", "b"],
        node_turn_lease=lease,
        model_concurrency_controller=controller,
    )

    assert response.content == "ok"
    assert calls == ["a", "b"]
    # a 的发请求时刻：准入 permit 正被这次 attempt 用着。
    # b 的发请求时刻：a 那颗已归还，没有虚挂。
    assert head_running == [1, 0]
    assert sum(int(v or 0) for v in controller.model_state("a")["running"].values()) == 0


@pytest.mark.asyncio
async def test_admission_permit_for_off_chain_model_stays_pinned_until_chat_end(monkeypatch) -> None:
    """现状的另一半，也是 Phase 3 必须避开的形状：准入绑定的模型不在实际发送的模型
    上时，那颗 permit 谁也不消费，一路空占到 `chat()` 的 finally 才放——在飞期间该
    模型的 `running` 被虚记一份。若把组选择只放在 chat 层而不动准入，每个回合都会
    以这种形状把负载虚挂在链首成员上。"""
    controller = _make_controller()
    lease = _lease_for(controller, "z")

    calls: list[str] = []
    z_running_during_b: list[int] = []

    def _observe_z() -> None:
        state = controller.model_state("z")
        z_running_during_b.append(sum(int(v or 0) for v in state["running"].values()))

    providers = {
        "a": _target(_AuthFailProvider("a", calls), model_key="a", retry_count=0),
        "b": _target(_OkProvider("b", calls, on_call=_observe_z), model_key="b", retry_count=0),
    }
    _patch_providers(monkeypatch, providers)

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["a", "b"],
        node_turn_lease=lease,
        model_concurrency_controller=controller,
    )

    assert response.content == "ok"
    assert calls == ["a", "b"]
    assert z_running_during_b == [1]
    assert sum(int(v or 0) for v in controller.model_state("z")["running"].values()) == 0


@pytest.mark.asyncio
async def test_first_attempt_consumes_admission_permit_only_when_chain_head_matches(monkeypatch) -> None:
    """现状的两半：链首与准入绑定同一个模型时，第一次 attempt 复用准入 permit（不
    二次 acquire）；不是同一个模型时，链首仍自行 acquire，准入那颗一路空占到结束。"""

    class _CountingController(ModelKeyConcurrencyController):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.acquire_calls: list[tuple[str, int]] = []

        async def acquire_specific(self, *, model_ref: str, key_index: int):
            self.acquire_calls.append((str(model_ref), int(key_index)))
            return await super().acquire_specific(model_ref=model_ref, key_index=key_index)

    # 情形一：准入模型 == 链首 -> 第一次 attempt 消费准入 permit。
    controller = _CountingController(
        resolve_model_limits=lambda model_ref: {"key_count": 1, "per_key_limit": 1, "key_indexes": [0]},
    )
    lease = _lease_for(controller, "a")
    calls: list[str] = []
    providers = {
        "a": _target(_OkProvider("a", calls), model_key="a", retry_count=0),
    }
    _patch_providers(monkeypatch, providers)

    await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["a"],
        node_turn_lease=lease,
        model_concurrency_controller=controller,
    )
    assert controller.acquire_calls == []

    # 情形二：准入模型不在链上 -> 链首自行 acquire，那颗准入 permit 空占到 finally。
    controller2 = _CountingController(
        resolve_model_limits=lambda model_ref: {"key_count": 1, "per_key_limit": 1, "key_indexes": [0]},
    )
    lease2 = _lease_for(controller2, "z")
    calls2: list[str] = []
    providers2 = {
        "a": _target(_OkProvider("a", calls2), model_key="a", retry_count=0),
    }
    _patch_providers(monkeypatch, providers2)

    await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["a"],
        node_turn_lease=lease2,
        model_concurrency_controller=controller2,
    )
    assert controller2.acquire_calls == [("a", 0)]
    assert sum(int(v or 0) for v in controller2.model_state("z")["running"].values()) == 0


@pytest.mark.asyncio
async def test_backoff_paces_same_model_rounds_only(monkeypatch) -> None:
    """现状（Phase 3 要在组内补节拍的那条）：退避只发生在同模型的轮之间，跨模型前
    进零等待。"""
    delays: list[int] = []

    def _record_backoff(attempt_number: int) -> float:
        delays.append(int(attempt_number))
        return 0.0

    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", _record_backoff)

    calls: list[str] = []
    providers = {
        "a": _target(_AlwaysRetryableProvider("a", calls), model_key="a", retry_count=2),
        "b": _target(_OkProvider("b", calls), model_key="b", retry_count=0),
    }
    _patch_providers(monkeypatch, providers)

    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=["a", "b"],
    )

    assert response.content == "ok"
    # retry_count=2 是**总轮预算**（判据 `rounds_used < budget_rounds`）：a 共发 2 轮，
    # 因此只产生第 1 轮后的一次退避；前进到 b 不再退避。
    assert delays == [1]
    assert calls == ["a", "a", "b"]


@pytest.mark.asyncio
async def test_retry_count_zero_uses_default_round_budget_not_one(monkeypatch) -> None:
    """现状陷阱：`retry_count=0`（含未配置）落 `DEFAULT_RETRYABLE_MODEL_ROUNDS`，不
    是 1 轮。group 预算要拿到「1 个完整 key pass」必须写显式值，不能复用该默认。"""
    monkeypatch.setattr(chat_backend_module, "model_retry_backoff_seconds", lambda attempt: 0.0)

    calls: list[str] = []
    providers = {
        "a": _target(_AlwaysRetryableProvider("a", calls), model_key="a", retry_count=0),
    }
    _patch_providers(monkeypatch, providers)

    with pytest.raises(RuntimeError):
        await _backend().chat(
            messages=[{"role": "user", "content": "demo"}],
            tools=None,
            model_refs=["a"],
        )

    assert len(calls) == fallback_module.DEFAULT_RETRYABLE_MODEL_ROUNDS
    assert fallback_module.DEFAULT_RETRYABLE_MODEL_ROUNDS == 10
