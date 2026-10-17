"""畸形 WAV 必须走 AudioError，不能把 struct.error 抛到"永不抛"的边界外面。

RIFF 走查里 fmt 块的守卫写的是 `end - body >= 14`，而 `<HHIIHH` 需要 16 字节：只要
fmt 的 body 被截到 14/15 字节，`struct.unpack` 就抛内建 `struct.error`。
`engine.transcribe_bytes` 只捕 `audio.AudioError`，注释写着 "Never raises"，
而这条读道的输入是桥与网页收到的**外部字节**。
"""

from __future__ import annotations

import struct

import pytest

from g3ku.stt.audio import AudioError, parse_wav


def _truncated_fmt_wav() -> bytes:
    """声明 fmt body = 16 字节，文件在 body 中途结束（只剩 14 字节），且后面没有 data 块。

    走查器把 `end` 夹到文件末尾 ⇒ `end - body == 14`：旧守卫 `>= 14` 放行，
    而 `struct.unpack("<HHIIHH", ...)` 需要 16 字节。
    """
    body = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    payload = b"WAVE" + b"fmt " + struct.pack("<I", 16) + body[:14]
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def test_truncated_fmt_raises_the_coded_audio_error() -> None:
    data = _truncated_fmt_wav()

    with pytest.raises(AudioError) as raised:
        parse_wav(data)

    assert raised.value.code in {"audio_not_wav", "audio_unreadable"}


def test_no_builtin_struct_error_escapes() -> None:
    """判据是"逃出的是哪一类"：AudioError 是 ValueError 子类，struct.error 不是。"""
    data = _truncated_fmt_wav()

    try:
        parse_wav(data)
    except AudioError:
        return
    except Exception as exc:  # noqa: BLE001 - 这里就是要抓非 AudioError 的逃逸
        pytest.fail(f"non-AudioError escaped parse_wav: {type(exc).__module__}.{type(exc).__name__}: {exc}")
    pytest.fail("truncated fmt chunk was accepted")
