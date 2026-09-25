"""Local speech-to-text for the web composer and inbound channel voice messages."""

from __future__ import annotations

from g3ku.stt.engine import (
    SttResult,
    inbound_voice_enabled,
    is_voice_payload,
    prepare_binary,
    prepare_model,
    status,
    transcribe_bytes,
)

__all__ = [
    "SttResult",
    "inbound_voice_enabled",
    "is_voice_payload",
    "prepare_binary",
    "prepare_model",
    "status",
    "transcribe_bytes",
]
