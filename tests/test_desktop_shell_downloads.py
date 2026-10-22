"""桌面壳必须自己打开下载：pywebview 的 ALLOW_DOWNLOADS 默认关，DownloadStarting
会被直接 args.Cancel，界面里的下载（导出配置包）就在窗口里静默消失。
"""

from __future__ import annotations

import pytest

from g3ku.shells.desktop import DOWNLOADS_SETTING, apply_shell_settings


def test_apply_shell_settings_turns_downloads_on() -> None:
    settings = {DOWNLOADS_SETTING: False}

    apply_shell_settings(settings)

    assert settings[DOWNLOADS_SETTING] is True


def test_pywebview_settings_object_accepts_the_flip() -> None:
    webview = pytest.importorskip("webview")

    # 默认是关的，所以壳侧那一行不是装饰；这里翻的必须是 pywebview 自己读的那份对象。
    assert webview.settings[DOWNLOADS_SETTING] is False

    apply_shell_settings(webview.settings)

    assert webview.settings[DOWNLOADS_SETTING] is True

    # 这份 settings 是 pywebview 的进程级全局，别把默认值留在被翻过的状态上交给后面的用例。
    webview.settings[DOWNLOADS_SETTING] = False
