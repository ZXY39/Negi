"""沙箱错误分类。

两类错误必须分开，否则模型会对着"沙箱坏了"反复重试，操作员也分不清是策略拦了还是
基础设施挂了：

* ``denied`` —— 策略/内核拦下了这次动作（模型该换路子）；
* ``unavailable`` —— 沙箱后端不可用（操作员该修环境）；
* ``runner_failure`` —— 后端自己在启动阶段崩了（同上，但读数不同）。

判定只认后端自己吐出的签名，**不猜模型文本**；签名表与 DSH 的
``denialSignatures`` / ``runnerFailureRules`` 同构，漏判的代价是分类不准，不能是放行。
"""

from __future__ import annotations

from typing import Any

KIND_DENIED = "denied"
KIND_UNAVAILABLE = "unavailable"
KIND_RUNNER_FAILURE = "runner_failure"
KIND_UNKNOWN = "unknown"


class SandboxError(RuntimeError):
    """沙箱层错误的基类；带一层可读的 kind，工具层据此给回执。"""

    kind = KIND_UNKNOWN

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.detail = dict(detail or {})


class SandboxUnavailableError(SandboxError):
    kind = KIND_UNAVAILABLE


class SandboxDeniedError(SandboxError):
    kind = KIND_DENIED


class SandboxPartialEnforcementError(SandboxUnavailableError):
    """后端可用但缺必需维度：只够 owner 档，不能服务第三方。"""


class SandboxRunnerFailureError(SandboxError):
    kind = KIND_RUNNER_FAILURE


# 内核/工具层拒绝的 stderr 签名。bwrap 自己把 EACCES 直接透传，所以这两条覆盖大多数。
_DENIAL_SIGNATURES: tuple[str, ...] = (
    "permission denied",
    "operation not permitted",
    "read-only file system",
)

# 后端启动失败的签名：namespace 建不起来、镜像/发行版缺失、二进制本身报错。
_RUNNER_FAILURE_SIGNATURES: tuple[str, ...] = (
    "bwrap: ",
    "failed to create new namespace",
    "no permissions to creating new namespace",
    "setting up uid map",
    "unshare failed",
    "sandbox-exec:",
    "exec format error",
    "no such file or directory: 'bwrap'",
)


def classify_result(*, exit_code: int | None, stderr: str) -> str:
    text = str(stderr or "").lower()
    if any(signature in text for signature in _RUNNER_FAILURE_SIGNATURES):
        return KIND_RUNNER_FAILURE
    if any(signature in text for signature in _DENIAL_SIGNATURES):
        return KIND_DENIED
    if exit_code is None:
        return KIND_UNAVAILABLE
    return KIND_UNKNOWN


def denied(message: str, *, detail: dict[str, Any] | None = None) -> SandboxDeniedError:
    return SandboxDeniedError(message, detail=detail)


def unavailable(message: str, *, detail: dict[str, Any] | None = None) -> SandboxUnavailableError:
    return SandboxUnavailableError(message, detail=detail)


__all__ = [
    "KIND_DENIED",
    "KIND_RUNNER_FAILURE",
    "KIND_UNAVAILABLE",
    "KIND_UNKNOWN",
    "SandboxDeniedError",
    "SandboxError",
    "SandboxPartialEnforcementError",
    "SandboxRunnerFailureError",
    "SandboxUnavailableError",
    "classify_result",
    "denied",
    "unavailable",
]
