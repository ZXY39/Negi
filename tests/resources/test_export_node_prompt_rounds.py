from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from main.runtime.node_prompt_contract import is_node_dynamic_contract_message


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _script_path() -> Path:
    return _repo_root() / "scripts" / "export_node_prompt_rounds.py"


def _dynamic_contract_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    matched: list[dict[str, object]] = []
    for message in list(messages or []):
        if is_node_dynamic_contract_message(dict(message or {})):
            matched.append(message)
    return matched


def test_export_node_prompt_rounds_script_exports_two_consecutive_requests(tmp_path: Path) -> None:
    output_dir = tmp_path / "node-prompt-rounds"
    result = subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "--output-dir",
            str(output_dir),
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    round1 = json.loads((output_dir / "round1.request.json").read_text(encoding="utf-8"))
    round2 = json.loads((output_dir / "round2.request.json").read_text(encoding="utf-8"))
    round1_payload = json.loads((output_dir / "round1.dynamic_contract.json").read_text(encoding="utf-8"))
    round2_payload = json.loads((output_dir / "round2.dynamic_contract.json").read_text(encoding="utf-8"))
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))

    assert summary["round_count"] == 2

    round1_contracts = _dynamic_contract_messages(list(round1.get("messages") or []))
    round2_contracts = _dynamic_contract_messages(list(round2.get("messages") or []))

    assert len(round1_contracts) == 0
    # 尾块退役：请求体里不再有动态合同消息。
    assert len(round2_contracts) == 0

    assert round1_payload == {}
    assert round2_payload == {}

    assert summary["rounds"][0]["dynamic_contract_message_indexes"] == []
    assert summary["rounds"][1]["dynamic_contract_message_indexes"] == []
    # 尾块退役后请求以真实对话消息收尾（这一轮是工具结果），不再由合同块占末位。
    assert summary["rounds"][1]["last_message_role"] == "tool"
    assert summary["rounds"][1]["dynamic_contract_message_count"] == 0

    round1_model_messages = json.loads((output_dir / "round1.model_messages.json").read_text(encoding="utf-8"))
    round2_model_messages = json.loads((output_dir / "round2.model_messages.json").read_text(encoding="utf-8"))

    assert _dynamic_contract_messages(list(round1_model_messages or [])) == []
    assert _dynamic_contract_messages(list(round2_model_messages or [])) == []

    round2_roles = [str((message or {}).get("role") or "") for message in list(round2.get("messages") or [])]
    assert "assistant" in round2_roles
    assert "tool" in round2_roles
