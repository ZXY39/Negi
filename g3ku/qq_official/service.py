"""Lifecycle shell for one official QQ bot account.

One AppID is one account: it owns a bridge id (``qq-official-<appId>``), an
auto-provisioned ``externalApi.tokens`` credential, one botpy connection task
and one coarse status. The botpy-bound network runtime lives in ``bridge.py``
and is only imported when a start actually happens, so the shell (and the web
runtime that holds it) has no hard dependency on ``qq-botpy``.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from typing import Any

from loguru import logger

from g3ku.config.loader import load_config, save_config
from g3ku.config.schema import ExternalApiTokenConfig, QqBotAccountConfig
from g3ku.qq_official.messages import bridge_id_for_app_id

# bridge 意外崩溃（如 botpy 登录瞬时失败抛 Robot(None) 的 AttributeError）后的
# 自动重连退避，节奏对齐 bridge.py 的 pump 重连（1s→60s 封顶）。崩溃前健康运行
# 超过阈值则把退避重置回起始值，避免长期健康后的一次崩溃被永久放大。
_BRIDGE_RETRY_INITIAL_BACKOFF_SECONDS = 1.0
_BRIDGE_RETRY_MAX_BACKOFF_SECONDS = 60.0
_BRIDGE_RETRY_HEALTHY_RUN_SECONDS = 60.0


class QqOfficialService:
    """Per-account bridge lifecycle. The registry that owns instances of this
    class lives in ``g3ku/shells/web.py`` and reconciles it against config."""

    def __init__(self, *, app_id: str, base_url: str | None = None):
        self._app_id = str(app_id or "").strip()
        self._bridge_id = bridge_id_for_app_id(self._app_id)
        self._base_url = str(base_url or "").rstrip("/")
        self._task: asyncio.Task | None = None
        self._state = "stopped"
        self._detail = ""
        self._config_signature = ""

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def bridge_id(self) -> str:
        return self._bridge_id

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "detail": self._detail}

    def _effective_base_url(self, cfg: Any) -> str:
        if self._base_url:
            return self._base_url
        try:
            port = int(getattr(cfg.web, "port", 18790) or 18790)
        except Exception:
            port = 18790
        return f"http://127.0.0.1:{port}/api/v1"

    @staticmethod
    def _signature(app_id: str, account: QqBotAccountConfig | None, *, global_enabled: bool) -> str:
        if account is None:
            return ""
        # 密钥摘要必须进签名：签名相等时 sync 直接返回，改过 AppSecret 的号
        # 会永远停在旧凭证的重试循环里（旧签名只含 enabled/appId/sandbox，
        # 单号时代靠重启蒙对，多号并跑后"改密钥救号"是常规操作）。摘要只取
        # 前 12 位十六进制，密钥原文不进日志与状态。
        secret_digest = hashlib.sha256(str(account.app_secret or "").encode("utf-8")).hexdigest()[:12]
        return (
            f"{bool(global_enabled and account.enabled)}|{str(app_id).strip()}"
            f"|{bool(account.sandbox)}|{secret_digest}"
        )

    def _set(self, state: str, detail: str = "") -> None:
        self._state = state
        self._detail = str(detail or "").strip()

    async def sync_from_config(self, *, account: QqBotAccountConfig | None, global_enabled: bool) -> None:
        cfg = load_config()
        if account is None or not str(self._app_id or "").strip():
            self._set("not_configured")
            await self.stop()
            return
        if not global_enabled or not account.enabled:
            # 状态要区分"总开关关"和"这一号被停用"，否则管理面上一号停用会把整列
            # 报成未启用，看不出别的号其实配好了。
            self._set("enabled_off" if not global_enabled else "account_disabled")
            await self.stop()
            return
        if not str(account.app_secret or "").strip():
            self._set("not_configured", "缺少 AppSecret（屏蔽后需先解锁）")
            await self.stop()
            return

        signature = self._signature(self._app_id, account, global_enabled=global_enabled)
        if signature == self._config_signature and self._task is not None and not self._task.done():
            return  # config unchanged and still running

        self._config_signature = signature
        self._base_url = self._effective_base_url(cfg)
        await self._restart(account)

    async def _restart(self, account: QqBotAccountConfig) -> None:
        await self.stop()
        token = self._ensure_bridge_token()
        self._set("connecting")
        self._task = asyncio.create_task(
            self._run(account, token),
            name=f"qq-official-bridge:{self._bridge_id}",
        )

    async def _run(self, account: QqBotAccountConfig, token: str) -> None:
        from g3ku.qq_official.bridge import run_qq_official_bridge

        backoff = _BRIDGE_RETRY_INITIAL_BACKOFF_SECONDS
        while True:
            started = time.monotonic()
            try:
                await run_qq_official_bridge(
                    app_id=self._app_id,
                    app_secret=str(account.app_secret or "").strip(),
                    sandbox=bool(account.sandbox),
                    token=token,
                    base_url=self._base_url,
                    on_state=self._set,
                )
                # bridge 干净返回是它自己报过的环境类终态（如 botpy 缺失），不重试。
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the service must survive bridge crashes
                if time.monotonic() - started >= _BRIDGE_RETRY_HEALTHY_RUN_SECONDS:
                    backoff = _BRIDGE_RETRY_INITIAL_BACKOFF_SECONDS
                logger.exception(
                    "qq-official bridge {} crashed; retrying in {:.0f}s", self._bridge_id, backoff
                )
                self._set("error", f"{exc}（将在 {backoff:.0f}s 后重试）")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, _BRIDGE_RETRY_MAX_BACKOFF_SECONDS)

    async def stop(self) -> None:
        """Cancel the bridge task. 绝不清 ``_config_signature``：``_restart`` 先写
        签名再 ``await stop()``，此处清空会让下一次 sync 永远把健康的桥判成"配置变了"
        （实盘表现为每条入站消息都把自家 pump 建 0.3s 后取消，回复从此无人消费）。
        重启/复活由 ``_task is None`` 与 ``_task.done()`` 判定，不依赖签名被抹掉。"""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _ensure_bridge_token(self) -> str:
        cfg = load_config()
        tokens = cfg.external_api.tokens or {}
        entry = tokens.get(self._bridge_id)
        existing = str(getattr(entry, "token", "") or "").strip()
        if existing:
            return existing
        token = secrets.token_urlsafe(32)
        cfg.external_api.tokens[self._bridge_id] = ExternalApiTokenConfig(
            token=token,
            label=f"官方 QQ 机器人 {self._app_id}",
            enabled=True,
        )
        save_config(cfg)
        return token
