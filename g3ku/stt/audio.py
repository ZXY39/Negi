"""Audio normalization for local speech-to-text.

The vendored ``whisper-cli`` binary reads RIFF/WAVE itself (its loader resamples
rate and downmixes channels), so this module has three jobs:

* hand WAV bytes through and report how long they really are,
* convert anything else (browser WebM/Opus, container-wrapped voice notes)
  into PCM WAV through ``ffmpeg``, and
* measure level, so an accidentally empty recording is rejected here instead of
  costing a full decode window.

Kept free of numpy and any decoding library on purpose: the web composer
records straight to WAV, so the hot path never needs a decoder, and the
fallback only costs a dependency on machines that really do receive non-WAV
audio.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass
from typing import Any

WAV_MAGIC = b"RIFF"
WAVE_MAGIC = b"WAVE"

# ffmpeg 兜底预算：语音条只有几十秒，超过这个时间就是卡死或恶意输入。
_FFMPEG_TIMEOUT_SECONDS = 20.0

# 低于此 RMS 判为"什么都没录到"。实测：数字静音 -120 dBFS，-3 dB 底噪 -47.8，
# 2% 音量的语音 -57.6，正常语音 -23.6 / -16.9。取 -70 只可能拒掉前两类，
# 给最轻的语音留了 12 dB 余量——它是省时间的粗筛，不是质量门。
SILENCE_RMS_DBFS = -70.0

_PCM_INT16 = 1
_IEEE_FLOAT32 = 3


class AudioError(ValueError):
    """Raised for audio this pipeline cannot use. ``code`` is the stable
    operator-facing identifier that surfaces verbatim in HTTP responses."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WavInfo:
    seconds: float
    sample_rate: int
    channels: int
    bits: int
    format_tag: int
    data_offset: int
    data_size: int


@dataclass(frozen=True)
class NormalizedAudio:
    wav_bytes: bytes
    info: WavInfo
    source_kind: str  # "wav" | "ffmpeg"

    @property
    def seconds(self) -> float:
        return self.info.seconds


def is_wav(data: bytes) -> bool:
    return len(data) > 12 and data[:4] == WAV_MAGIC and data[8:12] == WAVE_MAGIC


def parse_wav(data: bytes) -> WavInfo:
    """Walk the RIFF chunks ourselves instead of trusting the header's length.

    ffmpeg streaming to a pipe cannot seek back, so it writes a placeholder
    data size; ``wave.getnframes()`` then reports ~134217 s for a 23 s clip.
    The byte count of the actual chunk is the only number that can be trusted.
    """
    if not is_wav(data):
        raise AudioError("audio_not_wav", "不是 WAV 数据。")
    pos = 12
    fmt: dict[str, int] = {}
    data_offset = 0
    declared = 0
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        end = body + size
        if end > len(data):
            end = len(data)
        if chunk_id == b"fmt " and end - body >= 14:
            tag, channels, rate, _byte_rate, align, bits = struct.unpack("<HHIIHH", data[body:body + 16])
            fmt = {"format_tag": tag, "channels": channels, "sample_rate": rate, "align": align, "bits": bits}
        elif chunk_id == b"data":
            data_offset = body
            declared = size
        pos = body + size + (size & 1)
    if not fmt or not data_offset:
        raise AudioError("audio_unreadable", "WAV 缺少 fmt 或 data 块。")
    align = int(fmt["align"]) or 1
    available = max(0, len(data) - data_offset)
    data_size = min(declared, available) if 0 < declared <= available else available
    frames = data_size // align
    seconds = frames / float(fmt["sample_rate"] or 1)
    return WavInfo(
        seconds=seconds,
        sample_rate=int(fmt["sample_rate"]),
        channels=int(fmt["channels"]),
        bits=int(fmt["bits"]),
        format_tag=int(fmt["format_tag"]),
        data_offset=data_offset,
        data_size=data_size,
    )


def rms_dbfs(data: bytes, info: WavInfo) -> float | None:
    """Level of the first channel, or ``None`` when the sample format is one
    this function does not read (the silence gate is then simply skipped)."""
    block = data[info.data_offset:info.data_offset + info.data_size]
    channels = max(1, info.channels)
    if info.format_tag == _PCM_INT16 and info.bits == 16:
        samples = struct.unpack(f"<{len(block) // 2}h", block[: len(block) // 2 * 2])
        mono = samples[::channels]
        scaled = [value / 32768.0 for value in mono]
    elif info.format_tag == _IEEE_FLOAT32 and info.bits == 32:
        samples = struct.unpack(f"<{len(block) // 4}f", block[: len(block) // 4 * 4])
        scaled = list(samples[::channels])
    else:
        return None
    if not scaled:
        return None
    mean_square = sum(value * value for value in scaled) / len(scaled)
    if mean_square <= 0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(mean_square))


def _ffmpeg_binary() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise AudioError(
            "audio_decoder_missing",
            "该音频不是 WAV，需要 ffmpeg 解码，但本机未安装 ffmpeg。",
        )
    return found


def _scrubbed_env() -> dict[str, str]:
    """Minimal environment for the decoder child: it needs no credential, and
    inheriting this process's environment would hand every API key the runtime
    holds to a third-party binary."""
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "LANG")
    return {key: value for key in keep if (value := os.environ.get(key))}


def decode_to_wav(data: bytes, *, filename: str = "", mime_type: str = "") -> NormalizedAudio:
    """Return PCM WAV plus parsed geometry. Raises ``AudioError`` with a stable
    code when the input is empty, unreadable, or needs a decoder that is not
    installed."""
    if not data:
        raise AudioError("audio_empty", "音频内容为空。")
    if is_wav(data):
        return NormalizedAudio(wav_bytes=data, info=parse_wav(data), source_kind="wav")

    command: list[Any] = [
        _ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            env=_scrubbed_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError("audio_decode_timeout", "音频解码超时。") from exc
    except OSError as exc:
        raise AudioError("audio_decode_failed", f"音频解码失败：{exc}") from exc

    wav_bytes = completed.stdout or b""
    if completed.returncode != 0 or not is_wav(wav_bytes):
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip()[-240:]
        raise AudioError(
            "audio_decode_failed",
            f"音频解码失败（{filename or mime_type or 'unknown'}）：{detail or f'exit {completed.returncode}'}",
        )
    return NormalizedAudio(wav_bytes=wav_bytes, info=parse_wav(wav_bytes), source_kind="ffmpeg")
