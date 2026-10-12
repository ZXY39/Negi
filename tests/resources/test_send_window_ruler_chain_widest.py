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


def test_send_window_ruler_takes_the_widest_candidate_on_the_chain(monkeypatch) -> None:
    """链首 39 万、后面那颗 69 万：尺子必须取 69 万，不然压缩永远不动手。

    实盘形状（2026-10-12 task:d07d706989b4）：尺子只读 `refs[0]`，请求攒到 392,742 仍
    没越过按 69 万算的触发线，而准入按 39 万判"没人塞得下"，整机队列被钉死 110 分钟。
    """
    _patch_builders(monkeypatch, {"m_a": 390000, "m_b": 690000})
    info = resolve_send_model_context_window_info(config=_config({"m_a": 390000, "m_b": 690000}), model_refs=["m_a", "m_b"])

    assert info.context_window_tokens == 690000
    # 身份字段仍按链首：换尺子不该顺手换掉"这次报的是哪颗模型"。
    assert info.model_key == "m_a"
    assert info.resolution_error == ""


def test_single_ref_chain_keeps_its_own_window(monkeypatch) -> None:
    _patch_builders(monkeypatch, {"m_a": 390000})
    info = resolve_send_model_context_window_info(config=_config({"m_a": 390000}), model_refs=["m_a"])
    assert info.context_window_tokens == 390000


def test_window_resolves_from_provider_record_when_catalog_entry_is_absent(monkeypatch) -> None:
    """模型目录里没有这一项（受管视图取不到）时，回落到记录那本，两者必须同值。"""
    _patch_builders(monkeypatch, {"m_a": 390000, "m_b": 690000})
    info = resolve_send_model_context_window_info(config=_config(None), model_refs=["m_a", "m_b"])
    assert info.context_window_tokens == 690000


def test_missing_window_reports_resolution_error(monkeypatch) -> None:
    _patch_builders(monkeypatch, {})
    info = resolve_send_model_context_window_info(config=_config(None), model_refs=["m_a"])
    assert info.context_window_tokens == 0
    assert info.resolution_error == "context_window_tokens_missing"
