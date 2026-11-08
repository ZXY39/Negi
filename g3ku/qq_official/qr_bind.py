"""QQ bot scan-to-bind (扫码注册) protocol client.

腾讯 QQ bot 团队为 AI harness 类接入提供的扫码绑定通道（参考实现是 npm 包
``@tencent-connect/qqbot-connector``，本模块按同一线协议直连同一批
``q.qq.com`` 端点；``source`` 留空，扫码页把接入方统一显示为"第三方机器人"）。
流程：

1. 本地生成 32 字节 AES key，POST ``/lite/create_bind_task`` 换 ``task_id``；
2. task id 拼出 ``connect.html`` URL，操作员用手机 QQ 扫码并在端内页授权机器人；
3. 轮询 ``/lite/poll_bind_result`` 到 COMPLETED，拿回 ``bot_appid`` 与用第 1 步
   的 key 做 AES-256-GCM 加密的 AppSecret（blob 布局 nonce(12)‖密文‖tag(16)，
   与 ``cryptography`` 的 AESGCM 接口一一对应），以及扫码者的 ``user_openid``
   ——该 AppID 下扫码人的 c2c 身份，正是 ``qqBot.accounts.<appId>.ownerOpenids``
   期望的命名空间，扫码因此同时完成 Master 注册。

边界（与 Master 绑定码同一纪律）：

* AES key 与解出的 AppSecret 只活在本进程：key 存进注册表等轮询回来解密，
  明文由管理端点直接写进 config 覆盖层链路，二者不出现在任何 HTTP 响应里。
* 任务只存内存、带短 TTL，进程重启即清空；二维码过期由轮询侧报 ``expired``，
  前端自动重建任务。
"""

from __future__ import annotations

import base64
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

# 生产环境固定 q.qq.com；腾讯另有 test.q.qq.com 沙箱宿主，本模块暂不暴露。
_QQ_BOT_BIND_HOST = "q.qq.com"
_CREATE_BIND_TASK_PATH = "/lite/create_bind_task"
_POLL_BIND_RESULT_PATH = "/lite/poll_bind_result"
_UPSTREAM_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# 任务槽 TTL 只管回收陈旧槽位；二维码在腾讯侧过期后轮询会报 expired，
# 前端收到即重建任务，这个 TTL 不参与二维码寿命的判断。
QR_BIND_TASK_TTL_SECONDS = 300

# poll 的 status 数值沿用 SDK 的 BindStatus：0 NONE / 1 PENDING 都当"等待"，
# 2 COMPLETED / 3 EXPIRED。
_POLL_STATUS_WAITING = "waiting"
_POLL_STATUS_COMPLETED = "completed"
_POLL_STATUS_EXPIRED = "expired"


class QrBindUpstreamError(RuntimeError):
    """腾讯侧不可用或协议形状不符；管理端点映射成 502。"""


class QrBindError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = str(code or "qr_bind_error")
        self.message = str(message or "")
        self.status_code = int(status_code)


@dataclass
class QrBindTask:
    task_id: str
    key: bytes
    expires_at_monotonic: float


@dataclass
class QrBindPollResult:
    status: str
    bot_app_id: str
    bot_encrypted_secret: str
    user_openid: str


def _bind_url(path: str) -> str:
    return f"https://{_QQ_BOT_BIND_HOST}{path}"


async def _post_json(url: str, payload: dict[str, Any], *, transport: Any = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT, transport=transport) as client:
            response = await client.post(url, json=payload, headers={"Accept": "application/json"})
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPStatusError as exc:
        raise QrBindUpstreamError(f"HTTP {exc.response.status_code} from {url}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise QrBindUpstreamError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(body, dict):
        raise QrBindUpstreamError("unexpected non-object response body")
    if int(body.get("retcode") or 0) != 0:
        raise QrBindUpstreamError(str(body.get("msg") or f"retcode {body.get('retcode')}"))
    return body


async def create_bind_task(*, transport: Any = None) -> QrBindTask:
    """生成绑定任务；key 只在本地持有，腾讯用它加密回传的 AppSecret。"""
    key = secrets.token_bytes(32)
    body = await _post_json(_bind_url(_CREATE_BIND_TASK_PATH), {"key": base64.b64encode(key).decode("ascii")}, transport=transport)
    task_id = str((body.get("data") or {}).get("task_id") or "").strip()
    if not task_id:
        raise QrBindUpstreamError("create_bind_task: missing task_id")
    return QrBindTask(
        task_id=task_id,
        key=key,
        expires_at_monotonic=time.monotonic() + QR_BIND_TASK_TTL_SECONDS,
    )


def build_connect_url(task_id: str, source: str = "") -> str:
    """扫码 URL；``source`` 留空即"第三方机器人"，露出平台名要走商务渠道。"""
    return (
        f"https://{_QQ_BOT_BIND_HOST}/qqbot/openclaw/connect.html"
        f"?task_id={task_id}&source={source}&_wv=2"
    )


async def poll_bind_result(task_id: str, *, transport: Any = None) -> QrBindPollResult:
    body = await _post_json(_bind_url(_POLL_BIND_RESULT_PATH), {"task_id": str(task_id or "")}, transport=transport)
    data = body.get("data") or {}
    raw_status = int(data.get("status") or 0)
    if raw_status == 2:
        status = _POLL_STATUS_COMPLETED
    elif raw_status == 3:
        status = _POLL_STATUS_EXPIRED
    else:
        status = _POLL_STATUS_WAITING
    return QrBindPollResult(
        status=status,
        bot_app_id=str(data.get("bot_appid") or "").strip(),
        bot_encrypted_secret=str(data.get("bot_encrypt_secret") or ""),
        user_openid=str(data.get("user_openid") or "").strip(),
    )


def decrypt_secret(encrypted_b64: str, key: bytes) -> str:
    """解密腾讯回传的 AppSecret；blob 即 nonce‖密文‖tag，AESGCM 直接消费。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        blob = base64.b64decode(str(encrypted_b64 or ""))
    except Exception as exc:  # noqa: BLE001 - base64 解码失败按协议错误处理
        raise QrBindError("qr_bind_secret_unreadable", "扫码结果密文不是合法 base64") from exc
    if len(blob) <= 28:  # nonce(12) + tag(16) 之外至少还要有一字节密文
        raise QrBindError("qr_bind_secret_unreadable", "扫码结果密文长度不足")
    try:
        plain = AESGCM(bytes(key)).decrypt(blob[:12], blob[12:], None)
        return plain.decode("utf-8")
    except QrBindError:
        raise
    except Exception as exc:  # noqa: BLE001 - 密钥不配对/密文被改/编码畸形都进这里
        raise QrBindError("qr_bind_secret_unreadable", "扫码结果密文解密失败") from exc


def render_qr_svg_data_url(url: str) -> str:
    """扫码 URL → SVG data URL。二维码在宿主进程生成：客户端 bundle 不引第三方
    库，页面拿到的就是可信源编码的图像，没有伪造面。"""
    import io

    import qrcode
    import qrcode.image.svg

    buffer = io.BytesIO()
    qr = qrcode.QRCode(border=2)
    qr.add_data(str(url or ""))
    qr.make(fit=True)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    image.save(buffer, kind="SVG")
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class QqQrBindRegistry:
    """单活跃扫码任务槽：新任务直接替换旧任务，TTL 懒过期。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: QrBindTask | None = None

    def store(self, task: QrBindTask) -> None:
        with self._lock:
            self._active = task

    def get(self, task_id: str) -> QrBindTask | None:
        with self._lock:
            task = self._active
            if task is None or task.task_id != str(task_id or "").strip():
                return None
            if task.expires_at_monotonic <= time.monotonic():
                self._active = None
                return None
            return task

    def consume(self, task_id: str) -> bool:
        with self._lock:
            if self._active is None or self._active.task_id != str(task_id or "").strip():
                return False
            self._active = None
            return True

    def clear(self) -> None:
        with self._lock:
            self._active = None


_REGISTRY = QqQrBindRegistry()


def get_qq_qr_bind_registry() -> QqQrBindRegistry:
    return _REGISTRY


def reset_qq_qr_bind_registry() -> None:
    _REGISTRY.clear()  # test helper


__all__ = [
    "QR_BIND_TASK_TTL_SECONDS",
    "QrBindError",
    "QrBindPollResult",
    "QrBindTask",
    "QrBindUpstreamError",
    "QqQrBindRegistry",
    "build_connect_url",
    "create_bind_task",
    "decrypt_secret",
    "get_qq_qr_bind_registry",
    "poll_bind_result",
    "render_qr_svg_data_url",
    "reset_qq_qr_bind_registry",
]
