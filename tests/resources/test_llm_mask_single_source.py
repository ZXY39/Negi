"""掩码只有一个口径：任何"读回来再写回去"的调用方不能把真实 key 覆盖成掩码字符串。

读面（config 记录、绑定行、runtime config 导出、脱敏后的 NormalizedProviderConfig）与写回
闸门此前分用 `"***"` 与 `"********"` 两个字面量。闸门只认后者，于是走前者那条读道的调用方
一回写，明文 api key 就被字面量掩码替换掉。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from g3ku.llm_config.enums import AuthMode, Capability, ProtocolAdapter
from g3ku.llm_config.export import MASKED_SECRET_VALUE, to_generic_runtime_config
from g3ku.llm_config.facade import LLMConfigFacade
from g3ku.llm_config.models import NormalizedProviderConfig
from g3ku.llm_config.service import _redact_config

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_KEY = "sk-real-secret-do-not-overwrite"


def _config() -> NormalizedProviderConfig:
    now = datetime.now(UTC)
    return NormalizedProviderConfig(
        config_id="cfg-mask",
        provider_id="openai",
        display_name="mask test",
        protocol_adapter=ProtocolAdapter.OPENAI_COMPLETIONS,
        capability=Capability.CHAT,
        auth_mode=AuthMode.API_KEY,
        base_url="https://example.invalid/v1",
        default_model="gpt-test",
        template_version="1",
        parameters={"context_window_tokens": 200000},
        created_at=now,
        updated_at=now,
        auth={"type": "api_key", "api_key": REAL_KEY},
    )


def test_redacted_surfaces_emit_the_guarded_value() -> None:
    exported = to_generic_runtime_config(_config(), include_secrets=False)
    assert exported.auth["api_key"] == MASKED_SECRET_VALUE

    redacted = _redact_config(_config())
    assert redacted.auth["api_key"] == MASKED_SECRET_VALUE

    with_secrets = to_generic_runtime_config(_config(), include_secrets=True)
    assert with_secrets.auth["api_key"] == REAL_KEY


def test_writing_a_read_back_mask_keeps_the_real_key() -> None:
    facade = object.__new__(LLMConfigFacade)
    current = _config()
    patch = {
        "api_key": to_generic_runtime_config(current, include_secrets=False).auth["api_key"],
        "api_base": "https://example.invalid/v1",
        "provider_model": "gpt-test",
    }

    draft = facade._merge_draft(current, patch, replace_parameters=False)

    assert draft.api_key == REAL_KEY


def test_no_shipped_module_carries_a_second_mask_literal() -> None:
    offenders: list[str] = []
    for rel in ("g3ku", "main"):
        root = REPO_ROOT / rel
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.split("\n"), start=1):
                if re.search(r"""["'](\\?\*){3,4}["']""", line) and not re.search(r"MASKED_SECRET_VALUE\s*=", line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {line.strip()[:70]}")
    assert not offenders, "mask literals must reference MASKED_SECRET_VALUE: " + "; ".join(offenders)
