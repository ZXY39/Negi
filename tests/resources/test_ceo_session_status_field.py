"""侧栏黄环的取数侧合同：目录条目要带运行档位原值，且档位缺失时落到空串。

``is_running`` 是"在飞标志 ∪ status==running"的派生值，压不出"等审批挂起"和
"错误停止"的区别，所以 ``status`` 与它并列下发；黄环的判据在前端
（``org_graph_app.ceo_session_attention_ring.test.js``），这里只管字段活着走完
解析器 → 条目 → 补丁这条道。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from g3ku.runtime.api.ceo_sessions import _session_is_running, _session_status
from g3ku.runtime.web_ceo_sessions import list_local_ceo_sessions

REPO_ROOT = Path(__file__).resolve().parents[2]
CSS_PATH = REPO_ROOT / "g3ku" / "web" / "frontend" / "org_graph.css"


class _FakeSession:
    def __init__(self, key: str) -> None:
        self.key = key
        self.metadata: dict = {}
        self.messages: list = []


class _FakeTranscriptStore:
    def list_sessions(self):
        return [{"key": "web:run"}, {"key": "web:err"}, {"key": "web:gone"}]

    def get_or_create(self, key: str):
        return _FakeSession(key)


def _statuses(rows):
    return {row["session_id"]: row.get("status") for row in rows}


def test_catalog_items_carry_runtime_status_and_normalize_it():
    resolver = {"web:run": "RUNNING ", "web:err": "Error", "web:gone": None}.get
    rows = list_local_ceo_sessions(
        _FakeTranscriptStore(),
        active_session_id="web:run",
        is_running_resolver=lambda key: key == "web:run",
        status_resolver=resolver,
    )
    assert _statuses(rows) == {"web:run": "running", "web:err": "error", "web:gone": ""}


def test_catalog_status_survives_a_missing_or_raising_resolver():
    rows = list_local_ceo_sessions(
        _FakeTranscriptStore(),
        active_session_id="web:run",
        status_resolver=lambda key: (_ for _ in ()).throw(RuntimeError("runtime gone")),
    )
    assert set(_statuses(rows).values()) == {""}
    assert all("status" in row for row in rows)


def test_session_status_reads_the_runtime_state_alongside_is_running():
    manager = SimpleNamespace(
        get=lambda key: {
            "web:err": SimpleNamespace(state=SimpleNamespace(status=" error ", is_running=False)),
            "web:hot": SimpleNamespace(state=SimpleNamespace(status="idle", is_running=True)),
        }.get(key)
    )
    assert _session_status(manager, "web:err") == "error"
    assert _session_status(manager, "web:hot") == "idle"
    # 没建过运行会话（渠道归档、已回收）不能编出档位。
    assert _session_status(manager, "web:gone") == ""
    # is_running 仍按"在飞标志 ∪ running"算，两档各说各的事。
    assert _session_is_running(manager, "web:hot") is True
    assert _session_is_running(manager, "web:err") is False


def _rule(css: str, selector: str) -> str:
    escaped = re.escape(selector)
    match = re.search(escaped + r"\s*\{(?P<body>[^}]+)\}", css)
    assert match, f"缺少规则块：{selector}"
    return match.group("body")


def test_attention_ring_is_themed_and_does_not_breathe():
    css = CSS_PATH.read_text(encoding="utf-8")

    light = re.search(r":root\s*\{(?P<body>[^}]+)\}", css).group("body")
    dark = re.search(r'\[data-theme="dark"\]\s*\{(?P<body>[^}]+)\}', css).group("body")
    light_token = re.search(r"--ceo-session-attention-ring:\s*([^;]+);", light).group(1).strip()
    dark_token = re.search(r"--ceo-session-attention-ring:\s*([^;]+);", dark).group(1).strip()
    assert light_token and dark_token and light_token != dark_token, "黄环要按明暗各定一档"

    ring = _rule(css, ".ceo-session-card.is-attention::after")
    assert "--ceo-session-attention-ring" in ring
    assert "animation: none;" in ring, "呼吸在这套界面里表示『还在跑』，异常停住不许呼吸"

    # 折叠轨道只剩一颗字母块：卡片级描边收起，环搬到 glyph 上。
    collapsed_card = _rule(
        css,
        ".ceo-session-panel[data-panel-state=\"collapsed\"] .ceo-session-card.is-attention::after",
    )
    assert "opacity: 0;" in collapsed_card
    collapsed_glyph = _rule(
        css,
        ".ceo-session-panel[data-panel-state=\"collapsed\"] .ceo-session-card.is-attention .ceo-session-glyph::after",
    )
    assert "--ceo-session-attention-ring" in collapsed_glyph
    assert "animation: none;" in collapsed_glyph
