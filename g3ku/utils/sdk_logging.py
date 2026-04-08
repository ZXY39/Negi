from __future__ import annotations

import logging
import os
import sys
from typing import Mapping, TextIO

_OPENAI_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
}
_OPENAI_LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
_OPENAI_LOG_DATE_FORMAT = "%m/%d/%y %H:%M:%S"


def _resolve_openai_log_level(env: Mapping[str, str] | None = None) -> int | None:
    source = env if env is not None else os.environ
    raw = str(source.get("OPENAI_LOG", "") or "").strip().lower()
    return _OPENAI_LOG_LEVELS.get(raw)


def _plain_sdk_handler(stream: TextIO) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.NOTSET)
    handler.setFormatter(logging.Formatter(_OPENAI_LOG_FORMAT, _OPENAI_LOG_DATE_FORMAT))
    return handler


def configure_openai_sdk_logging(
    *,
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> None:
    """Route OpenAI/httpx logs to a predictable plain-text handler.

    `OPENAI_LOG=info|debug` enables the SDK's stdlib logger. In hosts that already
    attach rich or otherwise width-sensitive root handlers, those records can wrap
    into one-character lines. Installing a dedicated plain-text handler keeps the
    output stable and avoids leaking through arbitrary root logger configuration.
    """

    level = _resolve_openai_log_level(env)
    if level is None:
        return

    target_stream = stream if stream is not None else sys.stderr
    for logger_name in ("openai", "httpx"):
        logger = logging.getLogger(logger_name)
        logger.handlers = [_plain_sdk_handler(target_stream)]
        logger.setLevel(level)
        logger.propagate = False

