"""本地语音识别的音频层与引擎信封。

钉住四件实盘上真出过问题或必须成立的事：
1. 时长以实际字节数为准——ffmpeg 管道输出的 WAV 头部长度是占位值，
   信它会把 23 秒录音算成 134218 秒并被时长上限误拒；
2. 静音在进二进制之前就被拒（实测 -l zh 会把 3 秒静音听成两句话）；
3. 所有失败都以信封返回而不是抛异常（QQ 混排消息还要把文字半边送出去）；
4. 下载物校验摘要不过就拒绝解压。
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import zipfile
from pathlib import Path

import pytest

from g3ku.config.schema import Config, SttConfig
from g3ku.stt import audio, engine


def build_wav(samples: bytes, *, declared_size: int | None = None, rate: int = 16000, channels: int = 1) -> bytes:
    """Craft a 16-bit PCM WAV; ``declared_size`` lets the header lie on purpose."""
    align = channels * 2
    byte_rate = rate * align
    data_size = len(samples)
    header_size = declared_size if declared_size is not None else data_size
    body = struct.pack("<HHIIHH", 1, channels, rate, byte_rate, align, 16)
    return (
        b"RIFF"
        + struct.pack("<I", min(36 + header_size, 0xFFFFFFFF))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + body
        + b"data"
        + struct.pack("<I", header_size)
        + samples
    )


def sine_samples(seconds: float, *, rate: int = 16000, amplitude: float = 0.3) -> bytes:
    frames = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * 220 * index / rate)))
        for index in range(frames)
    )


def make_cfg(tmp_path: Path, **overrides) -> Config:
    cfg = Config()
    enabled = bool(overrides.pop("enabled", True))
    cfg.stt = SttConfig(enabled=enabled, model_dir=str(tmp_path), **overrides)
    return cfg


def provision(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir(parents=True, exist_ok=True)
    (tmp_path / "bin" / "whisper-cli.exe").write_bytes(b"placeholder")
    (tmp_path / "ggml-base.bin").write_bytes(b"placeholder-model")


# --- audio ---------------------------------------------------------------


def test_parse_wav_uses_real_data_size_when_header_lies():
    samples = sine_samples(2.0)
    lying = build_wav(samples, declared_size=0xFFFFFFFF)
    info = audio.parse_wav(lying)
    assert info.seconds == pytest.approx(2.0, abs=0.01)


def test_parse_wav_reads_truncated_stream():
    samples = sine_samples(1.0)
    truncated = build_wav(samples, declared_size=len(samples) + 4096)
    assert audio.parse_wav(truncated).seconds == pytest.approx(1.0, abs=0.01)


def test_rms_separates_silence_from_quiet_speech():
    silence = build_wav(b"\x00" * 32000)
    quiet = build_wav(sine_samples(1.0, amplitude=0.02))
    assert audio.rms_dbfs(silence, audio.parse_wav(silence)) == pytest.approx(-120.0)
    level = audio.rms_dbfs(quiet, audio.parse_wav(quiet))
    # -34dB 的语音必须留在门上：门只该挡"什么都没录到"。
    assert level is not None and level > engine.audio.SILENCE_RMS_DBFS


def test_decode_to_wav_rejects_empty_input():
    with pytest.raises(audio.AudioError) as empty:
        audio.decode_to_wav(b"")
    assert empty.value.code == "audio_empty"


# --- 腾讯 silk 语音条 ---------------------------------------------------


def _silk_bytes(seconds: float = 1.0) -> bytes:
    """用解码器自己的编码器合成一条语音条，避免把真实用户语音放进测试。"""
    import io

    import pysilk

    rate = 24000
    frames = int(seconds * rate)
    pcm = b"".join(
        struct.pack("<h", int(0.25 * 32767 * math.sin(2 * math.pi * 220 * i / rate)))
        for i in range(frames)
    )
    out = io.BytesIO()
    pysilk.encode(io.BytesIO(pcm), out, rate, 12000)
    return out.getvalue()


def test_is_tencent_silk_recognises_both_wrappers_and_rejects_other_audio():
    assert audio.is_tencent_silk(b"\x02" + audio.SILK_V3_MAGIC + b"...") is True
    assert audio.is_tencent_silk(audio.SILK_V3_MAGIC + b"...") is True
    # 平台把这类载荷的 content_type 写成 audio/mp3，所以只有字节判据可信。
    assert audio.is_tencent_silk(b"\xff\xfb\x90\x64fake-mp3") is False
    assert audio.is_tencent_silk(b"ID3\x03fake-mp3") is False


def test_silk_payloads_never_reach_the_ffmpeg_lane(monkeypatch):
    def _no_ffmpeg() -> str:
        raise AssertionError("silk 必须先于 ffmpeg 分流")

    monkeypatch.setattr(audio, "_ffmpeg_binary", _no_ffmpeg)
    silk = _silk_bytes(1.0)

    assert audio.is_tencent_silk(silk), "synthetic voice note must be silk (encoder changed?)"
    prepared = audio.decode_to_wav(silk, filename="qq-voice", mime_type="audio/mp3")
    assert prepared.source_kind == "silk"
    assert prepared.seconds == pytest.approx(1.0, abs=0.05)


def test_silk_decoder_missing_is_a_stable_code_not_a_crash(monkeypatch):
    monkeypatch.setitem(sys.modules, "pysilk", None)

    with pytest.raises(audio.AudioError) as exc:
        audio.decode_silk_to_wav(b"\x02" + audio.SILK_V3_MAGIC + b"\x00" * 40)
    assert exc.value.code == "audio_silk_decoder_missing"


def test_broken_silk_stream_reports_decode_failure(monkeypatch):
    class _DeadSilk:
        @staticmethod
        def decode(_inp, _out, _rate):
            raise RuntimeError("silk decoder rejected the stream")

    monkeypatch.setitem(sys.modules, "pysilk", _DeadSilk)

    with pytest.raises(audio.AudioError) as exc:
        audio.decode_silk_to_wav(b"\x02" + audio.SILK_V3_MAGIC + b"\x00" * 40)
    assert exc.value.code == "audio_decode_failed"
    assert "silk" in exc.value.message


# --- engine envelope -----------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_engine_never_touches_the_binary(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, enabled=False)
    called = []
    monkeypatch.setattr(engine, "_run_cli", lambda *a, **k: called.append(1))
    result = await engine.transcribe_bytes(build_wav(sine_samples(1.0)), cfg=cfg)
    assert result.ok is False
    assert result.error_code == "stt_disabled"
    assert not called


@pytest.mark.asyncio
async def test_missing_binary_and_model_report_actionable_codes(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(engine, "_run_cli", lambda *a, **k: "")
    missing_binary = await engine.transcribe_bytes(build_wav(sine_samples(1.0)), cfg=cfg)
    assert missing_binary.error_code == "stt_binary_missing"
    assert "g3ku stt prepare" in missing_binary.error

    (tmp_path / "bin").mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if Path(engine.binary_path(cfg).name).suffix == ".exe" else ""
    tmp_path.joinpath("bin", f"whisper-cli{suffix}").write_bytes(b"x")
    missing_model = await engine.transcribe_bytes(build_wav(sine_samples(1.0)), cfg=cfg)
    assert missing_model.error_code == "stt_model_missing"


@pytest.mark.asyncio
async def test_silence_is_rejected_before_the_binary_runs(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    provision(tmp_path)
    calls = []

    async def fake_run_cli(_cfg, _wav, _out, *, timeout):
        calls.append(timeout)
        return "我不该被调用"

    monkeypatch.setattr(engine, "_run_cli", fake_run_cli)
    result = await engine.transcribe_bytes(build_wav(b"\x00" * 96000), cfg=cfg)
    assert result.error_code == "stt_silent"
    assert result.wall_ms == 0
    assert not calls


@pytest.mark.asyncio
async def test_over_length_audio_is_rejected(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, max_audio_seconds=5)
    provision(tmp_path)
    monkeypatch.setattr(engine, "_run_cli", lambda *a, **k: "x")
    result = await engine.transcribe_bytes(build_wav(sine_samples(8.0)), cfg=cfg)
    assert result.error_code == "stt_too_long"
    assert result.seconds == pytest.approx(8.0, abs=0.1)


@pytest.mark.asyncio
async def test_traditional_output_is_simplified_and_timings_reported(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    provision(tmp_path)

    async def fake_run_cli(_cfg, _wav, _out, *, timeout):
        return "幫我查一下昨天定時任務的執行情況。\n"

    monkeypatch.setattr(engine, "_run_cli", fake_run_cli)
    result = await engine.transcribe_bytes(build_wav(sine_samples(1.0)), cfg=cfg)
    assert result.ok is True
    assert result.text == "帮我查一下昨天定时任务的执行情况。"
    assert result.wall_ms > 0
    assert result.model == "base"


@pytest.mark.asyncio
async def test_success_envelope_carries_the_decoded_wav(tmp_path, monkeypatch):
    """语音气泡回放的是引擎解码后的那一份 WAV，不是渠道送来的字节（QQ 的语音条是腾讯
    silk，浏览器播不了），所以成功结果必须把它带出去给进程内调用方。"""
    cfg = make_cfg(tmp_path)
    provision(tmp_path)

    async def fake_run_cli(_cfg, _wav, _out, *, timeout):
        return "刚刚给你发了啥"

    monkeypatch.setattr(engine, "_run_cli", fake_run_cli)
    wav = build_wav(sine_samples(1.0))
    result = await engine.transcribe_bytes(wav, cfg=cfg)

    assert result.ok is True
    assert result.wav_bytes == wav
    assert result.as_dict().get("wav_bytes") is None


@pytest.mark.asyncio
async def test_simplification_can_be_turned_off(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, simplify_chinese=False)
    provision(tmp_path)

    async def fake_run_cli(_cfg, _wav, _out, *, timeout):
        return "幫我查一下。\n"

    monkeypatch.setattr(engine, "_run_cli", fake_run_cli)
    result = await engine.transcribe_bytes(build_wav(sine_samples(1.0)), cfg=cfg)
    assert result.text == "幫我查一下。"


@pytest.mark.asyncio
async def test_empty_binary_output_becomes_stt_empty(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    provision(tmp_path)

    async def no_output(_cfg, _wav, _out, *, timeout):
        return "\n\n"

    monkeypatch.setattr(engine, "_run_cli", no_output)
    result = await engine.transcribe_bytes(build_wav(sine_samples(1.0)), cfg=cfg)
    assert result.error_code == "stt_empty"
    assert result.ok is False


@pytest.mark.asyncio
async def test_binary_crash_is_reported_not_raised(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    provision(tmp_path)

    async def boom(_cfg, _wav, _out, *, timeout):
        raise RuntimeError("whisper-cli 退出码 1")

    monkeypatch.setattr(engine, "_run_cli", boom)
    result = await engine.transcribe_bytes(build_wav(sine_samples(1.0)), cfg=cfg)
    assert result.ok is False
    assert result.error_code == "stt_failed"
    assert "退出码" in result.error


@pytest.mark.asyncio
async def test_slot_serializes_two_requests_and_second_one_gets_busy_code(tmp_path, monkeypatch):
    import asyncio

    cfg = make_cfg(tmp_path)
    provision(tmp_path)
    gate = asyncio.Event()

    async def slow_run_cli(_cfg, _wav, _out, *, timeout):
        await gate.wait()
        return "好。\n"

    monkeypatch.setattr(engine, "_run_cli", slow_run_cli)
    monkeypatch.setattr(engine, "_QUEUE_WAIT_SECONDS", 0.05)
    engine._slot_lock = None

    audio_bytes = build_wav(sine_samples(1.0))
    first = asyncio.create_task(engine.transcribe_bytes(audio_bytes, cfg=cfg))
    await asyncio.sleep(0.05)
    second = await engine.transcribe_bytes(audio_bytes, cfg=cfg)
    gate.set()
    await first
    assert second.error_code == "stt_busy"


def test_join_segments_keeps_cjk_glued_and_latin_spaced():
    assert engine._join_segments("第一句。\n第二句。\n") == "第一句。第二句。"
    assert (
        engine._join_segments("ask not what your country can do for you,\nask what you can do")
        == "ask not what your country can do for you, ask what you can do"
    )
    assert engine._join_segments("") == ""


# --- provisioning --------------------------------------------------------


def test_kept_member_drops_extra_executables_but_keeps_loader_deps():
    assert engine._kept_member("whisper-cli.exe") is True
    assert engine._kept_member("ggml-cpu-alderlake.dll") is True
    assert engine._kept_member("test-vad.exe") is False
    assert engine._kept_member("parakeet-cli.exe") is False


def test_extract_archive_only_materializes_kept_members(tmp_path):
    bundle = tmp_path / "whisper-bin-x64.zip"
    with zipfile.ZipFile(bundle, "w") as writer:
        writer.writestr("Release/whisper-cli.exe", b"cli")
        writer.writestr("Release/whisper.dll", b"dll")
        writer.writestr("Release/test-vad.exe", b"junk")
    target = tmp_path / "out"
    target.mkdir()
    kept = engine._extract_archive(bundle, target)
    assert sorted(kept) == ["whisper-cli.exe", "whisper.dll"]
    assert not (target / "test-vad.exe").exists()


class _FakeStreamResponse:
    def __init__(self, payload: bytes, status_code: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = dict(headers or {})

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size=0):
        for start in range(0, len(self._payload), max(1, chunk_size)):
            yield self._payload[start:start + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeClient:
    """Records the last request so the Range-resume behaviour is observable."""
    last_headers: dict[str, str] = {}
    response_headers: dict[str, str] = {}
    payload = b""
    status = 200

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url, headers=None, timeout=None):
        type(self).last_headers = dict(headers or {})
        return _FakeStreamResponse(
            type(self).payload, type(self).status, getattr(type(self), "response_headers", None)
        )


def test_prepare_binary_refuses_digest_mismatch(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, binary_sha256="0" * 64)
    monkeypatch.setattr(engine.httpx, "Client", _FakeClient)
    _FakeClient.payload = b"not-the-official-archive"
    _FakeClient.last_headers = {}
    monkeypatch.setattr(engine, "binary_dir", lambda _cfg: tmp_path / "bin")

    with pytest.raises(engine.SttProvisionError) as exc:
        engine.prepare_binary(cfg)
    assert "校验失败" in str(exc.value)
    assert not (tmp_path / "bin" / "whisper-cli.exe").exists()


def test_partial_download_resumes_with_a_range_request(tmp_path, monkeypatch):
    """A ~20KB/s link makes "restart from zero after a drop" a real cost, so the
    partial file must be extended, not overwritten."""
    cfg = make_cfg(tmp_path, binary_sha256="")
    directory = tmp_path / "bin"
    directory.mkdir(parents=True)
    asset = engine._platform_asset()
    archive = directory / asset
    archive.with_name(archive.name + ".part").write_bytes(b"HEAD")

    monkeypatch.setattr(engine, "binary_dir", lambda _cfg: directory)
    monkeypatch.setattr(engine.httpx, "Client", _FakeClient)
    _FakeClient.last_headers = {}
    _FakeClient.payload = b"-TAIL"
    _FakeClient.status = 206

    def fake_extract(_archive, _directory):
        (_directory / f"whisper-cli{'.exe' if os.name == 'nt' else ''}").write_bytes(b"cli")
        return ["whisper-cli"]

    monkeypatch.setattr(engine, "_extract_archive", fake_extract)

    result = engine.prepare_binary(cfg)

    assert _FakeClient.last_headers.get("Range") == "bytes=4-"
    assert result["downloaded"] is True
    assert not archive.with_name(archive.name + ".part").exists()


def test_download_progress_is_reported_per_chunk(tmp_path):
    """网页要能写出"下了多少"，所以每个数据块都得回一次话；Content-Length 缺失时
    total 是 None，界面退化成只报字节数而不是假装 0%。"""
    seen = []
    client = _FakeClient()
    _FakeClient.payload = b"abcde"
    _FakeClient.status = 200
    _FakeClient.response_headers = {"content-length": "5"}

    result = engine._stream_to(
        client, "https://example.test/thing", tmp_path / "thing.bin",
        on_progress=lambda done, total: seen.append((done, total)),
    )

    assert result.read_bytes() == b"abcde"
    assert seen[-1] == (5, 5)

    _FakeClient.response_headers = {}
    seen.clear()
    engine._stream_to(
        client, "https://example.test/thing2", tmp_path / "thing2.bin",
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen[-1] == (5, None)


def test_download_plan_says_the_measured_sizes(tmp_path):
    cfg = make_cfg(tmp_path, model="base")
    plan = engine.download_plan(cfg)
    assert plan["binary_bytes"] == engine._BINARY_DOWNLOAD_BYTES
    assert plan["model_bytes"] == engine._MODEL_DOWNLOAD_BYTES["base"]
    assert plan["model_bytes"] == 147951465, "与本机已下载的 ggml-base.bin 逐字节相符"

    target = engine.model_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    assert engine.download_plan(cfg)["model_bytes"] == 0


def test_stt_is_on_by_default():
    """新设备点麦克风应该走"下载"而不是"去改配置"：能力开关默认开，
    那 157 MB 的下载才是那道显式的门。"""
    assert Config().stt.enabled is True


def test_status_reports_readiness_across_all_three_gates(tmp_path):
    cfg = make_cfg(tmp_path)
    fresh = engine.status(cfg)
    assert fresh["ready"] is False
    assert fresh["binary_present"] is False

    provision(tmp_path)
    staged = engine.status(cfg)
    assert staged["binary_present"] is True
    assert staged["model_present"] is True
    assert staged["ready"] is True

    disabled = engine.status(make_cfg(tmp_path, enabled=False))
    assert disabled["binary_present"] is True
    assert disabled["ready"] is False


def test_result_envelope_is_json_serializable(tmp_path):
    payload = engine.SttResult(True, text="x", model="base", seconds=1.234, wall_ms=7).as_dict()
    assert json.loads(json.dumps(payload))["seconds"] == 1.23


def test_result_envelope_never_carries_the_decoded_wav(tmp_path):
    """``wav_bytes`` 是几百 KB 的二进制，只能进程内交给调用方；漏进 as_dict 就会被
    /ceo/transcribe 原样回给浏览器，还会被塞进会话快照。"""
    payload = engine.SttResult(True, text="x", model="base", wav_bytes=b"RIFF" + b"\x00" * 4096).as_dict()
    assert "wav_bytes" not in payload
    assert set(payload) == {"ok", "text", "error_code", "error", "model", "seconds", "wall_ms"}


# --- 存储压缩（语音气泡落盘的那一份） ------------------------------------


def test_encode_to_mp3_actually_shrinks_a_real_clip():
    """PCM 是 32 KB/秒，一条 60 秒上限的录音就是 2 MB。压缩是这条车道唯一
    让"可回放"变得可长期留存的理由，所以它必须真的把字节变小。"""
    if not audio.ffmpeg_available():
        pytest.skip("本机没有 ffmpeg：压缩按设计退回 WAV，不测")
    wav = build_wav(sine_samples(3.0))
    encoded = audio.encode_to_mp3(wav)
    assert encoded is not None
    assert encoded[:3] == b"ID3" or (encoded[0] == 0xFF and (encoded[1] & 0xE0) == 0xE0)
    assert len(encoded) < len(wav) / 4


def test_encode_to_mp3_declines_instead_of_raising(monkeypatch):
    """调用方拿 None 就是"原样留着 WAV"。这里任何一条分支都不许抛——
    转码失败把整条语音消息打回错误，比不压缩糟得多。"""
    assert audio.encode_to_mp3(b"definitely not a wav") is None

    monkeypatch.setattr(audio.shutil, "which", lambda _name: None)
    assert audio.encode_to_mp3(build_wav(sine_samples(0.2))) is None


def test_encode_to_mp3_rejects_a_failed_or_nonsense_encoder_run(monkeypatch):
    wav = build_wav(sine_samples(0.2))
    monkeypatch.setattr(audio.shutil, "which", lambda _name: "ffmpeg")

    class _Result:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = b"boom"

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        return _Result(1, b"")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    assert audio.encode_to_mp3(wav) is None

    monkeypatch.setattr(audio.subprocess, "run", lambda *a, **k: _Result(0, b"garbage"))
    assert audio.encode_to_mp3(wav) is None

    def timeout(*args, **kwargs):
        raise audio.subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    monkeypatch.setattr(audio.subprocess, "run", timeout)
    assert audio.encode_to_mp3(wav) is None
    assert calls


# --- 配置持久化 ---------------------------------------------------------


def test_runtime_payload_serializes_the_whole_stt_section(tmp_path):
    """`_runtime_config_payload` is an explicit whitelist: a field left out of
    it is silently dropped by every config save, so `--enable` would not stick."""
    from g3ku.config.loader import _runtime_config_payload

    cfg = make_cfg(tmp_path, enabled=True, model="small", threads=4, min_rms_dbfs=-88.5)
    payload = _runtime_config_payload(cfg)["stt"]

    assert payload["enabled"] is True
    assert payload["model"] == "small"
    assert payload["threads"] == 4
    assert payload["maxAudioSeconds"] == 60
    assert payload["minRmsDbfs"] == -88.5
    assert payload["binaryReleaseTag"] == "b5130"
    assert len(payload) == len(type(cfg.stt).model_fields), "stt gained a field the serializer does not write"


def test_existing_config_without_an_stt_block_still_loads(tmp_path):
    """Installs written before this section existed must keep starting: the
    explicit-fields guard may not demand an stt block from them."""
    from g3ku.config.loader import _ensure_runtime_fields_explicit, _runtime_config_payload

    cfg = make_cfg(tmp_path)
    raw = _runtime_config_payload(cfg)
    raw.pop("stt", None)

    _ensure_runtime_fields_explicit(raw, cfg)
    assert "stt" not in raw
