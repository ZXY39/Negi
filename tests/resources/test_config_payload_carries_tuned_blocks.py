"""可选旋钮必须活着经过一次保存。

`save_config` 用 `_runtime_config_payload` 整写 config.json（不是增量合并），白名单外
的键会在任意一次保存后被抹回 schema 默认值。`agents.nodeParallelism`、
`agents.toolDefaultTimeoutSeconds`、`mainRuntime.diskGuard` / `duplicatePrecheck` /
`eventHistory` 与 `updateCheck` 都是运行期会读的旋钮，所以按"调过才落盘"写入：
没调过的节不落盘（不给操作员文件加噪声），调过的必须能原样读回来。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from g3ku.config.loader import _runtime_config_payload, save_config
from g3ku.config.schema import Config


def _tuned_config() -> Config:
    cfg = Config()
    cfg.main_runtime.disk_guard.detail_retention_days = 7
    cfg.main_runtime.disk_guard.emergency_min_bytes = 123_456_789
    cfg.main_runtime.duplicate_precheck.llm_review_enabled = False
    cfg.main_runtime.event_history.archive_encoding = "plain"
    cfg.agents.tool_default_timeout_seconds = 60.0
    cfg.agents.node_parallelism.enabled = False
    cfg.agents.node_parallelism.max_parallel_tool_calls_per_node = 4
    cfg.update_check.enabled = False
    cfg.update_check.interval_hours = 9.0
    return cfg


def test_tuned_optional_knobs_are_written_by_the_payload() -> None:
    payload = _runtime_config_payload(_tuned_config())

    main_runtime = payload["mainRuntime"]
    assert main_runtime["diskGuard"] == {
        "emergencyMinBytes": 123_456_789,
        "detailRetentionDays": 7,
    }
    assert main_runtime["duplicatePrecheck"] == {"llmReviewEnabled": False}
    assert main_runtime["eventHistory"] == {"archiveEncoding": "plain"}
    assert payload["updateCheck"] == {"enabled": False, "intervalHours": 9.0}
    assert payload["agents"]["toolDefaultTimeoutSeconds"] == 60.0
    assert payload["agents"]["nodeParallelism"] == {"enabled": False, "maxParallelToolCallsPerNode": 4}


def test_untuned_optional_knobs_stay_out_of_the_file() -> None:
    payload = _runtime_config_payload(Config())

    assert "updateCheck" not in payload
    assert "nodeParallelism" not in payload["agents"]
    assert "toolDefaultTimeoutSeconds" not in payload["agents"]
    for block in ("diskGuard", "duplicatePrecheck", "eventHistory"):
        assert block not in payload["mainRuntime"]


def test_tuned_knobs_survive_a_saved_config_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".g3ku" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_config(_tuned_config(), path)

    reloaded = Config.model_validate(json.loads(path.read_text(encoding="utf-8")))

    assert reloaded.main_runtime.disk_guard.detail_retention_days == 7
    assert reloaded.main_runtime.disk_guard.emergency_min_bytes == 123_456_789
    assert reloaded.main_runtime.duplicate_precheck.llm_review_enabled is False
    assert reloaded.main_runtime.event_history.archive_encoding == "plain"
    assert reloaded.agents.tool_default_timeout_seconds == 60.0
    assert reloaded.agents.node_parallelism.enabled is False
    assert reloaded.agents.node_parallelism.max_parallel_tool_calls_per_node == 4
    assert reloaded.update_check.enabled is False
    assert reloaded.update_check.interval_hours == 9.0
