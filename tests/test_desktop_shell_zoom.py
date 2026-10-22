"""桌面窗口默认按 75% 打开：这版界面在 1280×860 的窗里 100% 偏大。

缩放走 WebView2 的 ZoomFactor（原生缩放，不是 CSS），所以壳必须在窗口就绪时设一次；
界面里的字号档位是另一个旋钮，只动阅读面。
"""

from __future__ import annotations

from g3ku.shells.desktop import DEFAULT_ZOOM_FACTOR, apply_zoom


class _Control:
    ZoomFactor = 1.0


def test_default_zoom_is_75_percent() -> None:
    assert DEFAULT_ZOOM_FACTOR == 0.75


def test_apply_zoom_sets_the_host_zoom_factor() -> None:
    control = _Control()

    line = apply_zoom(control)

    assert control.ZoomFactor == 0.75
    assert "75%" in line


def test_apply_zoom_reports_a_missing_control_instead_of_raising() -> None:
    class _Broken:
        def __setattr__(self, name: str, value: object) -> None:
            raise RuntimeError("no host control")

    line = apply_zoom(_Broken())

    assert "未生效" in line
    assert "RuntimeError" in line
