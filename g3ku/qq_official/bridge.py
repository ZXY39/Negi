"""botpy-bound network runtime for the QQ official adapter.

This is the ONLY module that imports ``qq-botpy``, and it does so lazily, so
the rest of g3ku has no hard dependency on it. Everything on the g3ku side is
done through the generic External Agent API over loopback (see
``g3ku/qq_official/client.py``); this file only wires botpy events to that
client and QQ message posting back out.

Real-device seam: the botpy event model field names and the exact
``api.post_*`` signatures are written to the official README's shape but must
be confirmed against a live AppID/AppSecret. All the logic reachable without a
QQ account (mapping, client, service, provisioning) is unit-tested elsewhere.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from loguru import logger

from g3ku.qq_official.client import ExternalApiClient
from g3ku.qq_official.messages import (
    OUTBOUND_EVENT,
    REPLY_DELTA_EVENT,
    REPLY_FINAL_EVENT,
    external_key_for_c2c,
    external_key_for_group,
    external_key_for_guild,
    external_key_for_guild_dm,
    idempotency_key_for,
    is_deliverable_event,
    parse_external_key,
)

StateCallback = Callable[[str, str], None]


async def run_qq_official_bridge(
    *,
    app_id: str,
    app_secret: str,
    sandbox: bool,
    token: str,
    base_url: str,
    on_state: StateCallback,
) -> None:
    """Run until cancelled/error. ``on_state`` reports coarse status."""
    try:
        import botpy
    except Exception as exc:  # noqa: BLE001
        on_state("error", f"botpy 未安装（pip install qq-botpy）: {exc}")
        return

    client = ExternalApiClient(base_url=base_url, token=token)
    # session bookkeeping: session_id -> external_key, and cached last SSE seq.
    sessions: dict[str, str] = {}
    seqs: dict[str, int] = {}
    pumps: set[asyncio.Task] = set()

    async def on_incoming(external_key: str, text: str, event_id: str) -> None:
        if not text.strip():
            return
        session_id = sessions.get(external_key)
        if session_id is None:
            session_id = await client.ensure_session(external_key)
            sessions[external_key] = session_id
            _spawn_pump(session_id, external_key)
        idem = idempotency_key_for(event_id)
        await client.send_message(session_id, text, idempotency_key=idem) if idem else await client.send_message(
            session_id, text, idempotency_key=external_key
        )

    async def deliver(external_key: str, text: str) -> None:
        kind, target = parse_external_key(external_key)
        if kind == "group":
            await bridge_api.post_group_message(group_openid=target["group_openid"], content=text, msg_type=0)
        elif kind == "c2c":
            await bridge_api.post_c2c_message(openid=target["user_openid"], content=text, msg_type=0)
        elif kind == "guild":
            await bridge_api.post_message(channel_id=target["channel_id"], content=text)
        elif kind == "guilddm":
            await bridge_api.post_dm(guild_id=target["guild_id"], msg_id="", content=text)
        else:
            logger.warning("qq-official cannot deliver to target external_key={}", external_key)

    async def _pump(session_id: str, external_key: str) -> None:
        seen = seqs.get(session_id, 0)
        try:
            async for event in client.stream_events(session_id, last_seq=seen):
                seqs[session_id] = int(event.get("seq") or seen)
                event_type = str(event.get("type") or "")
                if not is_deliverable_event(event_type):
                    continue
                text = str(event.get("text") or "").strip()
                if not text:
                    continue
                target_key = external_key
                if event_type == OUTBOUND_EVENT:
                    target_key = str(event.get("external_key") or external_key)
                await deliver(target_key, text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - reconnect loop keeps the pump alive
            logger.exception("qq-official event pump error for session {}", session_id)

    def _spawn_pump(session_id: str, external_key: str) -> None:
        task = asyncio.create_task(_pump(session_id, external_key), name=f"qq-official-pump:{session_id}")
        pumps.add(task)
        task.add_done_callback(pumps.discard)

    class QqOfficialClient(botpy.Client):
        async def on_ready(self):
            on_state("connected", "")

        async def on_group_at_message_create(self, message):
            await on_incoming(external_key_for_group(getattr(message, "group_openid", "")), _content_of(message), getattr(message, "id", ""))

        async def on_c2c_message_create(self, message):
            await on_incoming(external_key_for_c2c(_openid_of(message)), _content_of(message), getattr(message, "id", ""))

        async def on_at_message_create(self, message):
            await on_incoming(
                external_key_for_guild(getattr(message, "guild_id", ""), getattr(message, "channel_id", "")),
                _content_of(message),
                getattr(message, "id", ""),
            )

        async def on_direct_message_create(self, message):
            await on_incoming(
                external_key_for_guild_dm(getattr(message, "guild_id", ""), getattr(message, "author", {}).get("id", "")),
                _content_of(message),
                getattr(message, "id", ""),
            )

    def _content_of(message: Any) -> str:
        content = getattr(message, "content", "")
        return str(content or "").strip()

    def _openid_of(message: Any) -> str:
        author = getattr(message, "author", None) or {}
        return str(getattr(author, "user_openid", "") or getattr(author, "id", "") or "").strip()

    try:
        intents = botpy.Intents(public_guild_messages=True, direct_message=True, public_messages=True)
    except TypeError:
        intents = None

    bridge_api: Any = None

    try:
        bridge_client = QqOfficialClient(intents=intents) if intents is not None else QqOfficialClient()
        bridge_api = getattr(bridge_client, "api", None)
        on_state("connecting", "waiting for QQ gateway")
        await bridge_client.run(appid=app_id, secret=app_secret)  # blocks until stopped
    finally:
        for pump in list(pumps):
            pump.cancel()
        if pumps:
            await asyncio.gather(*pumps, return_exceptions=True)
        await client.close()
        on_state("stopped", "")