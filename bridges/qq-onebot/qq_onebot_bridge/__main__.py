"""Entry point: python -m qq_onebot_bridge [--config bridge.config.json]."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import load_bridge_config
from .dispatcher import Dispatcher
from .g3ku_client import G3kuClient
from .onebot import OnebotClient


async def run(config_path: str) -> None:
    config = load_bridge_config(config_path)
    g3ku = G3kuClient(config.g3ku.base_url, config.g3ku.token)
    onebot = OnebotClient(config.onebot.ws_url, config.onebot.http_url)

    bot_user_id = config.behavior.bot_user_id
    if not bot_user_id:
        try:
            bot_user_id = await onebot.get_login_user_id()
        except Exception:
            bot_user_id = 0

    dispatcher = Dispatcher(
        g3ku=g3ku,
        onebot=onebot,
        behavior=config.behavior,
        bot_user_id=bot_user_id,
    )
    stop = asyncio.Event()

    tasks = [
        asyncio.create_task(
            onebot.receive_events(
                on_event=dispatcher.handle_onebot_event,
                backoff_seconds=config.behavior.reconnect_backoff_seconds,
                stop=stop,
            )
        ),
        asyncio.create_task(dispatcher.progress_loop()),
    ]
    print(f"qq-onebot bridge started (bot_user_id={bot_user_id}); Ctrl+C to stop.")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        await dispatcher.stop()
        await g3ku.close()
        await onebot.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="G3KU QQ/OneBot reference bridge")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "bridge.config.json"),
        help="path to bridge.config.json",
    )
    args = parser.parse_args()
    if not Path(args.config).exists():
        raise SystemExit(
            f"config not found: {args.config} — copy bridge.config.example.json to bridge.config.json and fill it in."
        )
    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
