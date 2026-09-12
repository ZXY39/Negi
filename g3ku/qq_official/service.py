"""Lifecycle shell for the QQ official adapter.

Owns start/stop, a coarse status state machine, and the auto-provisioning of a
dedicated ``externalApi.tokens.qq-official`` credential. The botpy-bound
network runtime lives in ``bridge.py`` and is only imported when a start
actually happens, so the shell (and the web runtime that holds it) has no
hard dependency on ``qq-botpy``.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from loguru import logger

from g3ku.config.loader import load_config, save_config
from g3ku.config.schema import ExternalApiTokenConfig, QqBotConfig
from g3ku.qq_official.messages import QQ_BRIDGE_ID

# bridge 意外崩溃（如 botpy 登录瞬时失败抛 Robot(None) 的 AttributeError）后的
# 自动重连退避，节奏对齐 bridge.py 的 pump 重连（1s→60s 封顶）。崩溃前健康运行
# 超过阈值则把退避重置回起始值，避免长期健康后的一次崩溃被永久放大。
_BRIDGE_RETRY_INITIAL_BACKOFF_SECONDS = 1.0
_BRIDGE_RETRY_MAX_BACKOFF_SECONDS = 60.0
_BRIDGE_RETRY_HEALTHY_RUN_SECONDS = 60.0


class QqOfficialService:
    def __init__(self, base_url: str | None = None):
        self._base_url = str(base_url or "").rstrip("/")
        self._task: asyncio.Task | None = None
        self._state = "stopped"
        self._detail = ""
        self._config_signature = ""

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
    def _signature(q: QqBotConfig | None) -> str:
        if q is None:
            return ""
        return f"{bool(q.enabled)}|{str(q.app_id or '').strip()}|{bool(q.sandbox)}"

    def _set(self, state: str, detail: str = "") -> None:
        self._state = state
        self._detail = str(detail or "").strip()

    async def sync_from_config(self) -> None:
        cfg = load_config()
        q = getattr(cfg, "qq_bot", None)
        if q is None or not q.enabled:
            self._set("enabled_off" if q is not None else "not_configured")
            await self.stop()
            return
        if not str(q.app_id or "").strip() or not str(q.app_secret or "").strip():
            self._set("not_configured", "缺少 AppID 或 AppSecret（屏蔽后需先解锁）")
            await self.stop()
            return

        signature = self._signature(q)
        if signature == self._config_signature and self._task is not None and not self._task.done():
            return  # config unchanged and still running

        self._config_signature = signature
        self._base_url = self._effective_base_url(cfg)
        await self._restart(q)

    async def _restart(self, q: QqBotConfig) -> None:
        await self.stop()
        token = self._ensure_qq_official_token()
        self._set("connecting")
        self._task = asyncio.create_task(
            self._run(q, token),
            name="qq-official-bridge",
        )

    async def _run(self, q: QqBotConfig, token: str) -> None:
        from g3ku.qq_official.bridge import run_qq_official_bridge

        backoff = _BRIDGE_RETRY_INITIAL_BACKOFF_SECONDS
        while True:
            started = time.monotonic()
            try:
                await run_qq_official_bridge(
                    app_id=str(q.app_id or "").strip(),
                    app_secret=str(q.app_secret or "").strip(),
                    sandbox=bool(q.sandbox),
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
                logger.exception("qq-official bridge crashed; retrying in {:.0f}s", backoff)
                self._set("error", f"{exc}（将在 {backoff:.0f}s 后重试）")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, _BRIDGE_RETRY_MAX_BACKOFF_SECONDS)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _ensure_qq_official_token(self) -> str:
        cfg = load_config()
        tokens = cfg.external_api.tokens or {}
        entry = tokens.get(QQ_BRIDGE_ID)
        existing = str(getattr(entry, "token", "") or "").strip()
        if existing:
            return existing
        token = secrets.token_urlsafe(32)
        cfg.external_api.tokens[QQ_BRIDGE_ID] = ExternalApiTokenConfig(
            token=token,
            label="官方 QQ 机器人",
            enabled=True,
        )
        save_config(cfg)
        return token
