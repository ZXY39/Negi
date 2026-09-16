"""4a 模型链耗尽审计发射测试：工厂单点发射、六处调用点契约、瞬态重构不发射。"""

import json
from pathlib import Path

import pytest

from g3ku import audit_events
from g3ku.providers import fallback

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (_REPO_ROOT / path).read_text(encoding="utf-8")


def _lines(workspace: Path) -> list[dict]:
    audit_file = workspace / ".g3ku" / "audit.jsonl"
    if not audit_file.exists():
        return []
    raw = audit_file.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


@pytest.fixture()
def sink(tmp_path: Path):
    workspace = tmp_path / "workspace"
    audit_events.configure_audit_sink(workspace)
    yield workspace
    audit_events.configure_audit_sink(None)


def test_chain_exhaustion_factory_emits_one_event_and_keeps_behavior(sink: Path) -> None:
    error = fallback.exhausted_model_chain_error(
        RuntimeError("boom 429"), retry_on=[], model_chain=["provider:a", "provider:b"]
    )

    # 行为不变：仍返回 ModelProviderExhaustedError，错误文本照旧携带
    assert type(error).__name__ == "ModelProviderExhaustedError"
    assert "boom 429" in str(error.raw_message)

    lines = _lines(sink)
    assert len(lines) == 1
    record = lines[0]
    assert record["subsystem"] == "provider"
    assert record["level"] == "error"
    assert record["event_type"] == "provider_chain_exhausted"
    assert record["summary"].startswith("模型链耗尽：")
    assert record["detail"]["model_chain"] == ["provider:a", "provider:b"]
    assert "boom 429" in record["detail"]["error_text"]


def test_chain_exhaustion_retryable_flag_flows_into_detail(sink: Path) -> None:
    fallback.exhausted_model_chain_error(RuntimeError("rate limit"), retry_on=["rate limit"])
    record = _lines(sink)[0]
    assert record["detail"]["retryable"] is True


def test_chain_exhaustion_unconfigured_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    # 工厂单测（如 test_retry_keywords.py）从仓库根直接调用工厂：未配置绝不落盘
    monkeypatch.chdir(tmp_path)
    audit_events.configure_audit_sink(None)
    error = fallback.exhausted_model_chain_error(RuntimeError("boom"))
    assert type(error).__name__ == "ModelProviderExhaustedError"
    assert not (tmp_path / ".g3ku").exists()


def test_config_changed_error_emits_nothing(sink: Path) -> None:
    # 瞬态配置修订信号不进审计流
    error = fallback.retryable_chain_config_changed_error()
    assert error.config_revision_changed is True
    assert _lines(sink) == []


def test_all_raise_sites_pass_model_chain() -> None:
    fallback_src = _source("g3ku/providers/fallback.py")
    chat_backend_src = _source("main/runtime/chat_backend.py")
    assert fallback_src.count("model_chain=list(chain)") == 3
    assert chat_backend_src.count("model_chain=list(refs)") == 3
