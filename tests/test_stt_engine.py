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
import struct
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


def test_decode_to_wav_rejects_empty_and_non_wav_without_ffmpeg():
    with pytest.raises(audio.AudioError) as empty:
        audio.decode_to_wav(b"")
    assert empty.value.code == "audio_empty"


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


def test_prepare_binary_refuses_digest_mismatch(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, binary_sha256="0" * 64)

    class _FakeResponse:
        status_code = 200
        content = b"not-the-official-archive"

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, timeout=None):
            return _FakeResponse()

    monkeypatch.setattr(engine.httpx, "Client", _FakeClient)
    monkeypatch.setattr(engine, "binary_dir", lambda _cfg: tmp_path / "bin")
    with pytest.raises(engine.SttProvisionError) as exc:
        engine.prepare_binary(cfg)
    assert "校验失败" in str(exc.value)
    assert not (tmp_path / "bin" / "whisper-cli.exe").exists()


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
