"""请求工件用量聚合的进程内索引：重复调用不再重读，新增只解析增量。

`read_session_turn_token_usage` 在每次 CEO 快照构建时执行；大会话的请求目录有
300+ 个 ~1MB 工件，逐文件 json.loads 实测约 2s。索引按「目录文件集 + 已聚合
turns」缓存，本文件钉住四条失效与增量判据，以及结果与无缓存路径逐项相等。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from g3ku.runtime import web_ceo_sessions


@pytest.fixture(autouse=True)
def _fresh_usage_index():
    web_ceo_sessions.reset_turn_usage_index()
    yield
    web_ceo_sessions.reset_turn_usage_index()


def _write_artifact(directory: Path, name: str, record: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _count_parses(monkeypatch) -> list[str]:
    parses: list[str] = []
    original = web_ceo_sessions._load_request_artifact_usage

    def counting(path: Path):
        parses.append(path.name)
        return original(path)

    monkeypatch.setattr(web_ceo_sessions, "_load_request_artifact_usage", counting)
    return parses


def _use_directory(monkeypatch, mapping: dict[str, Path]) -> None:
    monkeypatch.setattr(
        web_ceo_sessions,
        "actual_request_dir_for_session",
        lambda session_id, create=False: mapping.get(str(session_id).strip()),
    )


def test_repeat_calls_do_not_reparse_existing_artifacts(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _write_artifact(directory, "000001.json", {"turn_id": "t1", "usage": {"input_tokens": 10, "output_tokens": 2}})
        _write_artifact(directory, "000002.json", {"turn_id": "t1", "usage": {"input_tokens": 5, "output_tokens": 3}})
        _write_artifact(directory, "000003.json", {"turn_id": "t2", "usage": {"input_tokens": 7, "output_tokens": 1}})
        _use_directory(monkeypatch, {"web:index": directory})
        parses = _count_parses(monkeypatch)

        first = web_ceo_sessions.read_session_turn_token_usage("web:index")
        assert len(parses) == 3
        assert first["t1"] == {
            "input_tokens": 15,
            "output_tokens": 5,
            "cache_hit_tokens": 0,
            "call_count": 2,
        }

        second = web_ceo_sessions.read_session_turn_token_usage("web:index")
        assert len(parses) == 3, "重复调用不应重读工件"
        assert second == first


def test_new_artifact_only_parses_the_delta(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _write_artifact(directory, "000001.json", {"turn_id": "t1", "usage": {"input_tokens": 10, "output_tokens": 2}})
        _use_directory(monkeypatch, {"web:index": directory})
        parses = _count_parses(monkeypatch)

        web_ceo_sessions.read_session_turn_token_usage("web:index")
        assert len(parses) == 1

        _write_artifact(directory, "000002.json", {"turn_id": "t1", "usage": {"input_tokens": 4, "output_tokens": 1}})
        updated = web_ceo_sessions.read_session_turn_token_usage("web:index")

        assert len(parses) == 2, "新增文件只应解析新增那一份"
        assert parses[1] == "000002.json"
        assert updated["t1"]["input_tokens"] == 14
        assert updated["t1"]["call_count"] == 2


def test_deleted_artifact_triggers_a_full_recompute(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        first_path = _write_artifact(directory, "000001.json", {"turn_id": "t1", "usage": {"input_tokens": 10}})
        second_path = _write_artifact(directory, "000002.json", {"turn_id": "t2", "usage": {"input_tokens": 20}})
        _use_directory(monkeypatch, {"web:index": directory})
        parses = _count_parses(monkeypatch)

        assert set(web_ceo_sessions.read_session_turn_token_usage("web:index")) == {"t1", "t2"}
        assert len(parses) == 2

        second_path.unlink()
        rebuilt = web_ceo_sessions.read_session_turn_token_usage("web:index")

        assert set(rebuilt) == {"t1"}
        assert len(parses) == 3, "删除文件后剩余文件要全量重算"
        assert first_path.exists()


def test_directory_change_triggers_a_full_recompute(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        first_dir = Path(tmp) / "first"
        second_dir = Path(tmp) / "second"
        _write_artifact(first_dir, "000001.json", {"turn_id": "t1", "usage": {"input_tokens": 10}})
        _write_artifact(second_dir, "000002.json", {"turn_id": "t9", "usage": {"input_tokens": 90}})
        mapping = {"web:index": first_dir}
        _use_directory(monkeypatch, mapping)
        parses = _count_parses(monkeypatch)

        assert set(web_ceo_sessions.read_session_turn_token_usage("web:index")) == {"t1"}

        mapping["web:index"] = second_dir
        rebuilt = web_ceo_sessions.read_session_turn_token_usage("web:index")

        assert set(rebuilt) == {"t9"}, "换了请求目录必须重算，不能沿用旧索引"
        assert len(parses) == 2


def test_indexed_result_matches_a_cold_aggregation(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _write_artifact(
            directory,
            "000001.json",
            {"turn_id": "t1", "usage": {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 500}},
        )
        _write_artifact(
            directory,
            "000002.json",
            {"turn_id": "t1", "usage": {"input_tokens": 80, "output_tokens": 30}},
        )
        _write_artifact(
            directory,
            "000003.json",
            {"turn_id": "t2", "usage": {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 0}},
        )
        _write_artifact(
            directory,
            "000004.json",
            {"turn_id": "t3", "usage": {"input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0}},
        )
        _use_directory(monkeypatch, {"web:index": directory})

        warm = web_ceo_sessions.read_session_turn_token_usage("web:index")
        web_ceo_sessions.reset_turn_usage_index()
        cold = web_ceo_sessions.read_session_turn_token_usage("web:index")

        assert warm == cold
        assert warm["t1"]["thinking_tokens"] == 500
        assert warm["t2"]["thinking_tokens"] == 0
        assert "t3" not in warm, "全零用量行保持不出现"
