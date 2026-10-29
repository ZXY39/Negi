"""桌面窗口默认按 90% 打开：这版界面在 1280×860 的窗里 100% 偏大。

缩放走 WebView2 的 ZoomFactor（原生缩放，不是 CSS），所以壳必须在窗口就绪时设一次；
设置里的「缩放」档位经 ShellApi.set_window_zoom 写同一个值，刷新后由 loaded 重放。
"""

from __future__ import annotations

from types import SimpleNamespace

from g3ku.shells.desktop import DEFAULT_ZOOM_FACTOR, ShellApi, apply_zoom


class _Control:
    ZoomFactor = 1.0

    def Invoke(self, callback) -> None:  # noqa: N802 - 模仿 WinForms 控件的同名方法
        callback()


def test_default_zoom_is_90_percent() -> None:
    assert DEFAULT_ZOOM_FACTOR == 0.9


def test_apply_zoom_sets_the_host_zoom_factor() -> None:
    control = _Control()

    line = apply_zoom(control)

    assert control.ZoomFactor == 0.9
    assert "90%" in line


def test_apply_zoom_reports_a_missing_control_instead_of_raising() -> None:
    class _Broken:
        def __setattr__(self, name: str, value: object) -> None:
            raise RuntimeError("no host control")

    line = apply_zoom(_Broken())

    assert "未生效" in line
    assert "RuntimeError" in line


def _window(control):
    return SimpleNamespace(native=SimpleNamespace(webview=control))


def test_shell_api_writes_the_zoom_through_the_window_control() -> None:
    control = _Control()
    api = ShellApi(_window(control))

    result = api.set_window_zoom(1.1)

    assert result["ok"] is True
    assert control.ZoomFactor == 1.1
    assert api.zoom_factor == 1.1


def test_shell_api_rejects_values_outside_the_slider_ends() -> None:
    control = _Control()
    api = ShellApi(_window(control))

    for bad in ("abc", "", 0, 0.05, 4):
        result = api.set_window_zoom(bad)
        assert result["ok"] is False
        assert control.ZoomFactor == 1.0
        assert api.zoom_factor == DEFAULT_ZOOM_FACTOR


def test_shell_api_replays_the_last_zoom_when_the_window_reloads() -> None:
    control = _Control()
    api = ShellApi(_window(control))
    assert api.set_window_zoom(1.2)["ok"] is True

    control.ZoomFactor = 1.0
    note = api._attach()

    assert control.ZoomFactor == 1.2
    assert "窗口缩放 120%" in note


def test_shell_api_reports_a_missing_host_control() -> None:
    class _Broken:
        @property
        def webview(self):
            raise RuntimeError("not on the UI thread")

    api = ShellApi(SimpleNamespace(native=_Broken()))

    result = api.set_window_zoom(1.1)

    assert result["ok"] is False
    assert "未生效" in result["error"]
    assert api.zoom_factor == DEFAULT_ZOOM_FACTOR
