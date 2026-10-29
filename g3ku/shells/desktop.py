"""桌面窗口壳：把本地 Web 界面装进一个原生窗口，不依赖用户的浏览器。

壳是无状态的——它只是指向已启动服务的一扇窗。关窗不停服务（停止仍是界面里那颗
退出按钮），再点一次图标就重新开一扇。承载方式是 WebView2：Windows 11 自带运行
时，所以壳不打包任何浏览器内核。

`http://127.0.0.1` 在 WebView2 里是安全上下文，`navigator.mediaDevices` 因此存在；
但麦克风权限要宿主点头，而 pywebview 不注册 `PermissionRequested`。这里的做法是在
`loaded` 回调里补挂——该回调由 WinForms 的 NavigationCompleted 触发，跑在 UI 线程
上，而 `CoreWebView2` 只允许在 UI 线程访问。

pywebview 的 `ALLOW_DOWNLOADS` 默认关，`DownloadStarting` 会被直接取消，界面里的下载
（导出配置包）就是这样在窗口里静默消失的。开壳前把它打开，下载就落到窗口自己的
「另存为」对话框上；浏览器轨道不受影响。

窗口按 `DEFAULT_ZOOM_FACTOR` 打开：这版界面在 100% 下对 1280×860 的窗偏大，缩放走
WebView2 的 `ZoomFactor`（原生缩放，不是 CSS），每次 `loaded` 重放最近一次的档位，所以
刷新后仍是同一比例。设置里的「缩放」档位写的就是这个 `ZoomFactor`：页面经
`ShellApi.set_window_zoom` 调进来（见 web-and-admin.md「Theme Persistence」）。
"""

from __future__ import annotations

from typing import Any

WINDOW_TITLE = "Negi"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 860
MICROPHONE_PERMISSION = "Microphone"
PERMISSION_ALLOWED = 1
DOWNLOADS_SETTING = "ALLOW_DOWNLOADS"
DEFAULT_ZOOM_FACTOR = 0.9
# 与设置里那根拖动条的两端一致：10% 与 300%。
ZOOM_MIN_FACTOR = 0.1
ZOOM_MAX_FACTOR = 3.0


def apply_shell_settings(settings: Any) -> None:
    """把壳需要的 pywebview 全局开关落到实处。"""
    settings[DOWNLOADS_SETTING] = True


def apply_zoom(control: Any, factor: float = DEFAULT_ZOOM_FACTOR) -> str:
    """把窗口缩放设到给定档；返回一句给操作员看的判读。"""
    try:
        control.ZoomFactor = float(factor)
    except Exception as exc:
        return f"窗口缩放未生效：{type(exc).__name__}"
    return f"窗口缩放 {round(factor * 100):d}%"


def _apply_zoom_on_ui_thread(window: Any, factor: float) -> str:
    """页面调进来的缩放在 pywebview 的工作线程上跑，改宿主控件必须回 UI 线程。"""
    try:
        control = window.native.webview
    except Exception as exc:
        return f"窗口缩放未生效：{type(exc).__name__}"
    notes: list[str] = []

    def _set() -> None:
        notes.append(apply_zoom(control, factor))

    invoke = getattr(control, "Invoke", None)
    if callable(invoke):
        try:
            import clr  # noqa: F401 - pythonnet 要先装好运行时，System 才导得进来
            from System import Action

            invoke(Action(_set))
        except Exception:
            _set()
    else:
        _set()
    return notes[0] if notes else "窗口缩放未生效：控制器未响应"


def shell_url(port: int | None = None) -> str:
    """界面地址。绑定地址可以是 0.0.0.0，可点击的地址只能是回环。"""
    resolved = port
    if resolved is None:
        from g3ku.config.loader import load_config

        try:
            resolved = int(load_config().web.port or 0)
        except Exception:
            resolved = 0
    if not resolved:
        resolved = 18790
    return f"http://127.0.0.1:{resolved}/"


def server_reachable(url: str, timeout: float = 2.0) -> bool:
    """窗口只对"已经在服务"的地址开，否则用户看到的是一页连接错误。"""
    import urllib.request

    probe = url.rstrip("/") + "/api/bootstrap/status"
    try:
        with urllib.request.urlopen(probe, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200) < 400
    except Exception:
        return False


def import_error() -> str:
    """能开壳返回空串，否则返回指明缺哪一环的一句话。"""
    import os
    import sys

    if sys.platform != "win32":
        return "桌面窗口目前只在 Windows 上提供；这台机器请用浏览器打开界面地址。"
    try:
        import webview  # noqa: F401
    except Exception as exc:
        return (
            f"桌面窗口需要 pywebview（已随 Windows 安装声明），当前导入失败：{exc}。"
            "可在安装目录执行 uv sync --frozen 补齐依赖。"
        )
    if not os.path.exists(os.path.join(sys.prefix, "Scripts", "pythonw.exe")):
        return "找不到 pythonw.exe，桌面窗口无法在无控制台状态下启动。"
    return ""


def _attach_microphone_permission(window: Any) -> str:
    """给 WebView2 补一个麦克风授权回调；返回一句给操作员看的判读。"""
    try:
        core = window.native.webview.CoreWebView2
    except Exception as exc:
        return f"未能接管权限回调，麦克风可能被静默拒绝：{type(exc).__name__}"

    def on_request(_sender: Any, args: Any) -> None:
        if str(args.PermissionKind) == MICROPHONE_PERMISSION:
            args.State = PERMISSION_ALLOWED

    try:
        core.PermissionRequested += on_request
    except Exception as exc:
        return f"权限回调注册失败：{type(exc).__name__}"
    return "麦克风权限已交给界面按需申请"


def _attach_window_controls(window: Any, factor: float = DEFAULT_ZOOM_FACTOR) -> str:
    """窗口就绪后一次性交出缩放与麦克风，拼成一行给操作员看。"""
    try:
        control = window.native.webview
    except Exception as exc:
        return f"未能接管窗口控制：{type(exc).__name__}"
    return f"{apply_zoom(control, factor)}；{_attach_microphone_permission(window)}"


class ShellApi:
    """页面能调的宿主开关。pywebview 在工作线程执行这些方法，碰 UI 控件要回 UI 线程。"""

    def __init__(self, window: Any = None, zoom_factor: float = DEFAULT_ZOOM_FACTOR) -> None:
        self._window = window
        self.zoom_factor = float(zoom_factor)

    def set_window_zoom(self, factor: Any) -> dict[str, Any]:
        try:
            value = float(factor)
        except (TypeError, ValueError):
            return {"ok": False, "error": "zoom must be a number"}
        if not ZOOM_MIN_FACTOR <= value <= ZOOM_MAX_FACTOR:
            return {
                "ok": False,
                "error": f"zoom must be between {ZOOM_MIN_FACTOR:g} and {ZOOM_MAX_FACTOR:g}",
            }
        note = _apply_zoom_on_ui_thread(self._window, value)
        if "未生效：" in note:
            return {"ok": False, "error": note}
        self.zoom_factor = value
        return {"ok": True, "zoom": value, "note": note}

    def _attach(self) -> str:
        """窗口就绪（UI 线程）时重放最近一次缩放并补挂麦克风权限。"""
        if self._window is None:
            return "未能接管窗口控制：窗口未创建"
        return _attach_window_controls(self._window, self.zoom_factor)


def run_desktop_shell(url: str | None = None, *, port: int | None = None) -> int:
    """开一扇指向本地服务的窗口，窗口关闭即返回。"""
    problem = import_error()
    if problem:
        print(f"[negi] {problem}")
        return 1

    import webview

    apply_shell_settings(webview.settings)

    target = url or shell_url(port)
    api = ShellApi()
    window = webview.create_window(
        WINDOW_TITLE,
        url=target,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        js_api=api,
    )
    api._window = window

    def on_loaded() -> None:
        print(f"[negi] 窗口已连接 {target}；{api._attach()}")

    window.events.loaded += on_loaded
    print(f"[negi] 正在打开桌面窗口 {target}")
    try:
        webview.start()
    except Exception as exc:
        print(f"[negi] 桌面窗口启动失败：{type(exc).__name__}: {exc}")
        print("[negi] 可以先用浏览器打开同一个地址：negi")
        return 1
    return 0
