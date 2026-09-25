"""Local speech-to-text over the official ``whisper.cpp`` CLI binary.

The engine shells out to ``whisper-cli`` once per request instead of keeping a
model resident, because that choice was measured rather than assumed. On this
product's floor (2 vCPU / 4 logical CPUs, 7.7 GB RAM, no GPU) with a 22.9s
Mandarin clip and the ``base`` model:

======================  =========  ==========  ==========
form                     23s clip   peak RSS   idle RSS
======================  =========  ==========  ==========
``whisper-cli`` process     6.9 s      318 MB      0 MB
resident ``whisper-server`` 6.2 s      274 MB    269 MB
======================  =========  ==========  ==========

Keeping a server alive buys 0.7 s because the cost is whisper's fixed 30-second
decode window, not model loading -- so the resident form pays 269 MB around the
clock for nothing, needs a supervisor, and adds a loopback port. The subprocess
route also means no Python-side ASR dependency at all.

Transcription is serialized by one global slot: one call already wants every
logical CPU we grant it, and two overlapping voices (a QQ note while the web
composer is in use, or several bot accounts at once) would only slow each other
and the agent turn down.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from g3ku.config.live_runtime import get_runtime_config
from g3ku.config.schema import Config
from g3ku.deployment.data_root import resolve_data_path
from g3ku.stt import audio
from g3ku.utils.process_tree import kill_process_tree

_MODEL_FILENAME = "ggml-{name}.bin"
_BINARY_ASSET = {"windows": "whisper-bin-x64.zip", "linux": "whisper-bin-ubuntu-x64.tar.gz"}
# 官方 zip 里除了 CLI 还有十几个测试/示例可执行文件，只留下载物必需的部分。
_SKIP_PREFIXES = ("test-", "parakeet", "bench", "command", "stream", "talk", "lsp", "quantize", "vad", "wchess")

# 排队预算：一个槽位本身最长就跑 timeout_seconds，第二条音频等满一个完整回合就
# 该被拒绝，而不是把 HTTP 请求无限挂下去。
_QUEUE_WAIT_SECONDS = 60.0

_LATIN_TAIL = re.compile(r"[A-Za-z0-9][^A-Za-z0-9]*$")
_LATIN_START = re.compile(r"^[A-Za-z0-9]")


class SttProvisionError(RuntimeError):
    """Raised by ``prepare_*`` for operator-facing provisioning failures."""


@dataclass(frozen=True)
class SttResult:
    """Uniform envelope: callers never special-case a backend, and a failed
    transcription is data, not an exception -- the QQ handler still has to
    deliver the text half of a mixed message."""

    ok: bool
    text: str = ""
    error_code: str = ""
    error: str = ""
    model: str = ""
    seconds: float = 0.0
    wall_ms: int = 0
    # Normalized, browser-playable WAV. QQ's original payload is Tencent silk,
    # which nothing can play, so a channel that wants a playbackable voice
    # bubble stores this instead of the bytes it sent us.
    wav_bytes: bytes = b""

    def as_dict(self) -> dict[str, Any]:
        """Wire shape. Deliberately excludes ``wav_bytes``: the clip is huge
        relative to the transcript and only in-process callers can take it."""
        return {
            "ok": self.ok,
            "text": self.text,
            "error_code": self.error_code,
            "error": self.error,
            "model": self.model,
            "seconds": round(self.seconds, 2),
            "wall_ms": self.wall_ms,
        }


_slot_lock: asyncio.Lock | None = None


def _get_slot_lock() -> asyncio.Lock:
    global _slot_lock
    if _slot_lock is None:
        _slot_lock = asyncio.Lock()
    return _slot_lock


def current_config() -> Config:
    config, _revision, _changed = get_runtime_config()
    return config


def stt_root(cfg: Config) -> Path:
    return resolve_data_path(cfg.stt.model_dir, default=".g3ku/stt")


def binary_dir(cfg: Config) -> Path:
    return stt_root(cfg) / "bin"


def binary_path(cfg: Config) -> Path:
    """Where ``whisper-cli`` lives: an explicit override, then the directory
    ``prepare_binary`` fills, then ``PATH``."""
    override = str(cfg.stt.binary_path or "").strip()
    if override:
        return Path(override).expanduser()
    suffix = ".exe" if os.name == "nt" else ""
    installed = binary_dir(cfg) / f"whisper-cli{suffix}"
    if installed.exists():
        return installed
    found = shutil.which(f"whisper-cli{suffix}") or shutil.which("whisper-cli")
    return Path(found) if found else installed


def model_path(cfg: Config) -> Path:
    return stt_root(cfg) / _MODEL_FILENAME.format(name=cfg.stt.model)


def converter_available() -> bool:
    return find_spec("zhconv") is not None


def is_voice_payload(data: bytes) -> bool:
    """Byte-level "this is a voice note" check for channels whose declared
    media type cannot be trusted (QQ sends ``content_type='voice'`` for a
    Tencent silk stream and labels the download ``audio/mp3``)."""
    return audio.is_tencent_silk(data)


def status(cfg: Config | None = None) -> dict[str, Any]:
    cfg = cfg or current_config()
    binary = binary_path(cfg)
    model = model_path(cfg)
    return {
        "enabled": bool(cfg.stt.enabled),
        "model": cfg.stt.model,
        "language": cfg.stt.language,
        "threads": int(cfg.stt.threads),
        "max_audio_seconds": int(cfg.stt.max_audio_seconds),
        "timeout_seconds": int(cfg.stt.timeout_seconds),
        "binary_path": str(binary),
        "binary_present": binary.is_file(),
        "model_path": str(model),
        "model_present": model.is_file(),
        "model_bytes": model.stat().st_size if model.is_file() else 0,
        "simplify_chinese": bool(cfg.stt.simplify_chinese),
        "converter_available": converter_available(),
        "ready": bool(cfg.stt.enabled and binary.is_file() and model.is_file()),
    }


async def inbound_voice_enabled(cfg: Config | None = None) -> bool:
    """Whether the QQ bridge should transcribe incoming audio instead of
    forwarding it as a file. One helper so both surfaces agree on "ready"."""
    cfg = cfg or current_config()
    return bool(status(cfg)["ready"])


def _join_segments(raw: str) -> str:
    """whisper's txt output is one segment per line. The newlines are not part
    of the utterance, but dropping them glues Latin words together
    ("...for you,ask..."), so a space goes in only at a latin/latin join."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return ""
    merged = lines[0]
    for line in lines[1:]:
        # 逗号/句号结尾的英文段之间补空格；补在字母上而不是标点上，
        # 中文段（标点是非 latin）保持直接相连。
        if _LATIN_TAIL.search(merged) and _LATIN_START.match(line):
            merged += " "
        merged += line
    return merged.strip()


def _simplify(cfg: Config, text: str) -> str:
    """whisper's base/tiny answered every Chinese clip in traditional
    characters on this box (small wrote simplified); conversion measured
    48 microseconds, so it is a post-pass rather than a model upgrade."""
    if not cfg.stt.simplify_chinese or not text:
        return text
    try:
        from zhconv import convert

        return convert(text, "zh-hans")
    except ImportError:
        return text
    except Exception as exc:  # noqa: BLE001 - cosmetic; never fail a turn over it
        logger.warning("stt traditional-to-simplified conversion skipped: {}", exc)
        return text


async def transcribe_bytes(
    data: bytes,
    *,
    filename: str = "",
    mime_type: str = "",
    source: str = "",
    cfg: Config | None = None,
) -> SttResult:
    """Transcribe one audio payload. Never raises."""
    cfg = cfg or current_config()
    started = time.perf_counter()
    model = cfg.stt.model
    snapshot = status(cfg)

    if not cfg.stt.enabled:
        return SttResult(False, error_code="stt_disabled", error="语音识别未启用。", model=model)
    if not snapshot["binary_present"]:
        return SttResult(
            False,
            error_code="stt_binary_missing",
            error=f"未找到 whisper-cli（{snapshot['binary_path']}），请先执行 g3ku stt prepare。",
            model=model,
        )
    if not snapshot["model_present"]:
        return SttResult(
            False,
            error_code="stt_model_missing",
            error=f"模型 {model} 未下载（{snapshot['model_path']}），请先执行 g3ku stt prepare。",
            model=model,
        )

    loop = asyncio.get_running_loop()
    try:
        prepared = await loop.run_in_executor(
            None,
            lambda: audio.decode_to_wav(data, filename=filename, mime_type=mime_type),
        )
    except audio.AudioError as exc:
        return SttResult(False, error_code=exc.code, error=exc.message, model=model)

    if prepared.seconds > float(cfg.stt.max_audio_seconds):
        return SttResult(
            False,
            error_code="stt_too_long",
            error=f"语音时长 {prepared.seconds:.0f} 秒，超过 {cfg.stt.max_audio_seconds} 秒上限。",
            model=model,
            seconds=prepared.seconds,
        )

    # 空录音先在这里拒掉：走完整解码要 5 秒以上，而且 whisper 对静音的态度取决于
    # 语言参数（实测 -l zh 会把 3 秒静音听成两句话），不能把正确性押在采样上。
    level = audio.rms_dbfs(prepared.wav_bytes, prepared.info)
    if level is not None and level < float(cfg.stt.min_rms_dbfs):
        return SttResult(
            False,
            error_code="stt_silent",
            error="没有录到声音（音频几乎是静音）。",
            model=model,
            seconds=prepared.seconds,
        )

    lock = _get_slot_lock()
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_QUEUE_WAIT_SECONDS)
    except TimeoutError:
        return SttResult(
            False,
            error_code="stt_busy",
            error="语音识别正在处理另一段音频，请稍候再试。",
            model=model,
            seconds=prepared.seconds,
        )

    workdir = Path(tempfile.mkdtemp(prefix="g3ku-stt-"))
    try:
        wav_path = workdir / "in.wav"
        wav_path.write_bytes(prepared.wav_bytes)
        raw = await _run_cli(cfg, wav_path, workdir / "out", timeout=float(cfg.stt.timeout_seconds))
        text = _simplify(cfg, _join_segments(raw))
        wall_ms = int((time.perf_counter() - started) * 1000)
        if not text:
            return SttResult(
                False,
                error_code="stt_empty",
                error="没有识别到有效语音内容（可能是静音或噪声）。",
                model=model,
                seconds=prepared.seconds,
                wall_ms=wall_ms,
            )
        logger.info(
            "stt transcribed source={} model={} audio_s={:.1f} wall_ms={} chars={}",
            source or "unknown",
            model,
            prepared.seconds,
            wall_ms,
            len(text),
        )
        return SttResult(
            True,
            text=text,
            model=model,
            seconds=prepared.seconds,
            wall_ms=wall_ms,
            wav_bytes=prepared.wav_bytes,
        )
    except Exception as exc:  # noqa: BLE001 - the envelope contract
        logger.exception("stt transcription failed")
        return SttResult(
            False,
            error_code="stt_failed",
            error=str(exc),
            model=model,
            seconds=prepared.seconds,
            wall_ms=int((time.perf_counter() - started) * 1000),
        )
    finally:
        lock.release()
        shutil.rmtree(workdir, ignore_errors=True)


async def _run_cli(cfg: Config, wav_path: Path, out_base: Path, *, timeout: float) -> str:
    argv = [
        str(binary_path(cfg)),
        "-m",
        str(model_path(cfg)),
        "-f",
        str(wav_path),
        "-l",
        str(cfg.stt.language or "zh"),
        "-t",
        str(max(1, int(cfg.stt.threads))),
        # -sns suppresses non-speech tokens. Measured: at the default best-of,
        # 3s of silence yields empty output; best-of 1 yields two fluent
        # sentences, so silence rejection depends on sampling staying default.
        "-sns",
        "-of",
        str(out_base),
        "-otxt",
    ]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError as exc:
        kill_process_tree(process)
        raise RuntimeError(f"语音识别超过 {timeout:.0f} 秒未完成，已终止。") from exc
    if process.returncode != 0:
        raise RuntimeError(f"whisper-cli 退出码 {process.returncode}")
    text_file = out_base.with_suffix(".txt")
    if not text_file.exists():
        return ""
    return text_file.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# provisioning: the request path never downloads anything
# --------------------------------------------------------------------------


def _platform_asset() -> str:
    return _BINARY_ASSET["windows" if os.name == "nt" else "linux"]


def _stream_to(client: httpx.Client, url: str, target: Path) -> Path:
    """Download into ``.part`` **incrementally**, appending as bytes arrive.

    This box reaches GitHub assets at ~20 KB/s, so minutes of progress must
    survive a dropped connection: buffering the response and writing it once
    would leave nothing on disk mid-download, and the `Range` request below
    would have no partial file to resume from.
    """
    part = target.with_name(target.name + ".part")
    existing = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={existing}-"} if existing else {}
    with client.stream("GET", url, headers=headers, timeout=httpx.Timeout(60.0, connect=10.0)) as response:
        if response.status_code == 416 and existing:
            # 已经拿全了：直接返回现有 .part 走摘要校验。
            return part
        response.raise_for_status()
        appending = response.status_code == 206
        if existing and not appending:
            logger.info("stt download restarts from zero (server ignored the Range request)")
        with part.open("ab" if appending else "wb") as handle:
            for chunk in response.iter_bytes(chunk_size=256 * 1024):
                handle.write(chunk)
    return part


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_model(cfg: Config | None = None) -> dict[str, Any]:
    cfg = cfg or current_config()
    target = model_path(cfg)
    if target.is_file() and target.stat().st_size > 0:
        return {"ok": True, "downloaded": False, "path": str(target), "bytes": target.stat().st_size}
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"{cfg.stt.model_download_base_url}/{target.name}"
    with httpx.Client(follow_redirects=True) as client:
        part = _stream_to(client, url, target)
    part.replace(target)
    return {"ok": True, "downloaded": True, "path": str(target), "bytes": target.stat().st_size}


def prepare_binary(cfg: Config | None = None) -> dict[str, Any]:
    """Unpack the pinned official build after verifying the archive digest --
    nothing here executes before that check passes."""
    cfg = cfg or current_config()
    suffix = ".exe" if os.name == "nt" else ""
    cli = binary_dir(cfg) / f"whisper-cli{suffix}"
    if cfg.stt.binary_path.strip():
        return {"ok": True, "downloaded": False, "path": str(binary_path(cfg)), "externally_managed": True}
    if cli.is_file():
        return {"ok": True, "downloaded": False, "path": str(cli)}

    asset = _platform_asset()
    url = f"{cfg.stt.binary_download_base_url}/{cfg.stt.binary_release_tag}/{asset}"
    cli.parent.mkdir(parents=True, exist_ok=True)
    archive = cli.parent / asset
    with httpx.Client(follow_redirects=True) as client:
        part = _stream_to(client, url, archive)
    digest = _sha256(part)
    expected = str(cfg.stt.binary_sha256 or "").strip().lower()
    if expected and digest != expected:
        part.unlink(missing_ok=True)
        raise SttProvisionError(f"下载包校验失败（sha256 {digest[:16]}…，期望 {expected[:16]}…），已拒绝解压。")
    part.replace(archive)
    try:
        kept = _extract_archive(archive, cli.parent)
    finally:
        archive.unlink(missing_ok=True)
    if not cli.is_file():
        raise SttProvisionError(f"解包后未找到 whisper-cli{suffix}（得到 {len(kept)} 个文件），请检查 {asset} 内容。")
    return {"ok": True, "downloaded": True, "path": str(cli), "files": len(kept)}


def _kept_member(name: str) -> bool:
    lowered = name.lower()
    if any(lowered.startswith(prefix) for prefix in _SKIP_PREFIXES):
        return False
    suffix = ".exe" if os.name == "nt" else ""
    if lowered in {f"whisper-cli{suffix}", "whisper.dll", "llama.dll", "ggml.dll", "SDL2.dll"}:
        return True
    return lowered.startswith("ggml-") or lowered.endswith(".so") or lowered.endswith(".dll")


def _extract_archive(archive: Path, directory: Path) -> list[str]:
    kept: list[str] = []
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                name = Path(member.filename).name
                if member.is_dir() or not _kept_member(name):
                    continue
                (directory / name).write_bytes(bundle.read(member))
                kept.append(name)
        return kept
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle.getmembers():
            name = Path(member.name).name
            if not member.isfile() or not _kept_member(name):
                continue
            source = bundle.extractfile(member)
            if source is None:
                continue
            (directory / name).write_bytes(source.read())
            kept.append(name)
    return kept
