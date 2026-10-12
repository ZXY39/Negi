from __future__ import annotations

from types import SimpleNamespace

import main.runtime.chat_backend as chat_backend_module
from main.runtime.chat_backend import resolve_send_model_context_window_info


def _patch_builders(monkeypatch, windows: dict[str, int]) -> None:
    """provider 构造那层只用来读记录里的窗口：造一个同形状的替身就够了。"""

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return SimpleNamespace(
            provider_id="custom",
            model_id=f"{model_key}-model",
            model_parameters={"context_window_tokens": int(windows.get(str(model_key)) or 0)},
        )

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)


def _config(windows: dict[str, int] | None):
    if windows is None:
        return SimpleNamespace(get_managed_model=lambda ref: None)
    return SimpleNamespace(
        get_managed_model=lambda ref: SimpleNamespace(context_window_tokens=int(windows.get(str(ref)) or 0))
        if str(ref) in windows
        else None
    )


def test_predicted_group_window_beats_the_chain_values(monkeypatch) -> None:
    """准入预测到这一跳落在 69 万那颗的组里：尺子就按那个组的最小成员取，不看整条链。

    实盘形状（2026-10-12 task:d07d706989b4）的反面：尺子按整条链最大取时，请求攒到
    392,742 仍不越线，而 39 万的成员已被准入筛出候选池（6 颗只剩 2 颗），最后没人塞得下。
    """
    _patch_builders(monkeypatch, {"m_a": 390000, "m_b": 690000})
    info = resolve_send_model_context_window_info(
        config=_config({"m_a": 390000, "m_b": 690000}),
        model_refs=["m_a", "m_b"],
        target_windows=[690000],
    )

    assert info.context_window_tokens == 690000
    # 身份字段仍按链首：换尺子不该顺手换掉"这次报的是哪颗模型"。
    assert info.model_key == "m_a"
    assert info.resolution_error == ""


def test_predicted_group_takes_its_smallest_member_not_the_largest(monkeypatch) -> None:
    """同一个组里 69 万与 39 万并存：按最小成员取，否则大成员会把小成员挤出候选池。"""
    _patch_builders(monkeypatch, {"m_a": 390000, "m_b": 690000})
    info = resolve_send_model_context_window_info(
        config=_config({"m_a": 390000, "m_b": 690000}),
        model_refs=["m_b"],
        target_windows=[690000, 390000],
    )
    assert info.context_window_tokens == 390000


def test_no_prediction_falls_back_to_the_chain_minimum(monkeypatch) -> None:
    """没有回合准入（前门、记忆车道）或预测判不出来时退回链级口径：取整条链最小，不是最大。"""
    _patch_builders(monkeypatch, {"m_a": 390000, "m_b": 690000})
    info = resolve_send_model_context_window_info(
        config=_config({"m_a": 390000, "m_b": 690000}),
        model_refs=["m_a", "m_b"],
    )
    assert info.context_window_tokens == 390000


def test_unwindowed_prediction_member_does_not_drag_the_ruler_to_zero(monkeypatch) -> None:
    """预测送来 0（那颗读不到窗口）时不能把尺子打成 0：0 会撞 `<= 25000` 硬门，等于让一条坏记录把节点全部后续跳拒发。"""
    _patch_builders(monkeypatch, {"m_a": 390000, "m_b": 690000})
    info = resolve_send_model_context_window_info(
        config=_config({"m_a": 390000, "m_b": 690000}),
        model_refs=["m_a", "m_b"],
        target_windows=[0, 690000],
    )
    assert info.context_window_tokens == 690000


def test_single_ref_chain_keeps_its_own_window(monkeypatch) -> None:
    _patch_builders(monkeypatch, {"m_a": 390000})
    info = resolve_send_model_context_window_info(config=_config({"m_a": 390000}), model_refs=["m_a"])
    assert info.context_window_tokens == 390000


def test_window_resolves_from_provider_record_when_catalog_entry_is_absent(monkeypatch) -> None:
    """模型目录里没有这一项（受管视图取不到）时，回落到记录那本，两者必须同值。"""
    _patch_builders(monkeypatch, {"m_a": 390000, "m_b": 690000})
    info = resolve_send_model_context_window_info(config=_config(None), model_refs=["m_a", "m_b"])
    assert info.context_window_tokens == 390000


def test_missing_window_reports_resolution_error(monkeypatch) -> None:
    _patch_builders(monkeypatch, {})
    info = resolve_send_model_context_window_info(config=_config(None), model_refs=["m_a"])
    assert info.context_window_tokens == 0
    assert info.resolution_error == "context_window_tokens_missing"


class _PeekController:
    def __init__(self, result, *, raises: bool = False) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, object]] = []

    def peek_hop_windows(self, *, node_id: str, route_plan: object) -> object:
        self.calls.append((str(node_id), route_plan))
        if self._raises:
            raise RuntimeError("balancer exploded")
        return self._result


def _loop_with(controller: object):
    from main.runtime.react_loop import ReActToolLoop

    loop = ReActToolLoop(chat_backend=object(), log_service=SimpleNamespace(), max_iterations=1)
    loop._node_turn_controller = controller
    return loop


def test_hop_ruler_carries_the_predicted_group_window_and_source() -> None:
    """尺子的两个字段都要往下传：窗口给压缩，来源给归因（帧里有，故障正文里没有）。"""
    plan = object()
    controller = _PeekController({"group_key": "group_1", "model_key": "m_small", "min_window": 390000})
    loop = _loop_with(controller)

    assert loop._peek_hop_ruler(node_id="node:1", route_plan=plan) == {
        "windows": [390000],
        "source": "group:group_1",
    }
    assert controller.calls == [("node:1", plan)]


def test_hop_ruler_stays_silent_when_admission_cannot_predict() -> None:
    """没有组链位、判不出窗口、甚至预测本身炸了：一律回 None 走链级回落，绝不把 0 当尺子传下去。"""
    for result in (None, {"group_key": "g", "model_key": "m", "min_window": 0}):
        assert _loop_with(_PeekController(result))._peek_hop_ruler(node_id="node:1", route_plan=object()) is None
    assert _loop_with(_PeekController(None))._peek_hop_ruler(node_id="node:1", route_plan=None) is None
    assert _loop_with(_PeekController(None, raises=True))._peek_hop_ruler(node_id="node:1", route_plan=object()) is None
