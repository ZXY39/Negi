from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from g3ku.resources.loader import ResourceLoader
from g3ku.resources.registry import ResourceRegistry
from g3ku.session.manager import SessionManager
from g3ku.session.transcript_search import open_message, search_messages

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = REPO_ROOT / "tools" / "search_session_history"


def _long_message() -> dict:
    return {
        "role": "assistant",
        "content": ("x" * 4000) + "关键词" + ("y" * 4000),
        "turn_id": "long",
        "timestamp": "2026-10-30T12:00:00",
    }


def _messages() -> list[dict]:
    return [
        {
            "role": "user",
            "content": "我们上次讨论预算 120 万，等待审批。",
            "timestamp": "2026-10-30T10:00:00",
            "metadata": {"_transcript_turn_id": "t1", "_transcript_state": "completed"},
        },
        {
            "role": "assistant",
            "content": "预算 120 万已经记录，审批在周五。",
            "turn_id": "t1",
            "timestamp": "2026-10-30T10:01:00",
        },
        {
            "role": "user",
            "content": "内部规则：每次都要检查预算",
            "timestamp": "2026-10-30T10:02:00",
            "metadata": {
                "_transcript_turn_id": "t2",
                "internal_prompt_kind": "heartbeat_rule",
                "ui_visible": False,
            },
        },
        {
            "role": "system",
            "content": "会话已压缩",
            "metadata": {"kind": "context_compression", "prompt_visible": False},
        },
        {
            "role": "assistant",
            "content": "临时归档片段 预算",
            "timestamp": "2026-10-30T10:03:00",
            "metadata": {"source": "follow_up_archive", "prompt_visible": False, "ui_visible": True},
        },
        {
            "role": "user",
            "content": "这一轮也提到预算",
            "timestamp": "2026-10-30T10:04:00",
            "metadata": {"_transcript_turn_id": "t3", "_transcript_state": "pending"},
        },
        {
            "role": "assistant",
            "content": "当前回合的回复也提到预算",
            "turn_id": "t3",
            "timestamp": "2026-10-30T10:05:00",
        },
        {
            "role": "user",
            "content": "",
            "attachments": [{"name": "预算表.xlsx", "path": "C:/tmp/预算表.xlsx"}],
            "timestamp": "2026-10-30T10:06:00",
            "metadata": {"_transcript_turn_id": "t4", "_transcript_state": "completed"},
        },
    ]


def test_search_defaults_exclude_internal_archive_and_current_turn():
    result = search_messages(
        _messages(),
        keywords=["预算"],
        current_turn_id="t3",
    )

    assert result["ok"] is True
    assert result["total_matches"] == 3
    assert [hit["turn_id"] for hit in result["hits"]] == ["t4", "t1", "t1"]
    assert result["hits"][0]["attachment_names"] == ["预算表.xlsx"]
    assert result["searched_messages"] == 3


def test_search_internal_and_current_turn_switches():
    messages = _messages()

    assert search_messages(messages, keywords=["预算"], current_turn_id="t3")["total_matches"] == 3
    assert (
        search_messages(
            messages, keywords=["预算"], current_turn_id="t3", include_internal=True
        )["total_matches"]
        == 5
    )
    assert (
        search_messages(
            messages, keywords=["预算"], current_turn_id="t3", include_current_turn=True
        )["total_matches"]
        == 5
    )
    assert (
        search_messages(
            messages,
            keywords=["预算"],
            current_turn_id="t3",
            include_internal=True,
            include_current_turn=True,
        )["total_matches"]
        == 7
    )


def test_search_supports_all_any_and_role_filter():
    messages = _messages()

    all_result = search_messages(messages, keywords=["预算", "审批"], match="all")
    assert all_result["total_matches"] == 2
    assert all("审批" in hit["matched_keywords"] for hit in all_result["hits"])

    any_result = search_messages(
        messages, keywords=["预算", "审批"], match="any", current_turn_id="t3"
    )
    assert any_result["total_matches"] == 3

    assistant_only = search_messages(
        messages, keywords=["预算"], roles=["assistant"], current_turn_id="t3"
    )
    assert assistant_only["total_matches"] == 1
    assert assistant_only["hits"][0]["turn_id"] == "t1"


def test_search_snippet_is_centered_on_match_in_a_long_line():
    result = search_messages(
        [_long_message()],
        keywords=["关键词"],
        snippet_chars=120,
    )

    hit = result["hits"][0]
    assert "关键词" in hit["snippet"]
    assert hit["snippet"].startswith("…")
    assert hit["snippet"].endswith("…")
    assert len(hit["snippet"]) <= 122


def test_search_pagination_uses_recent_order_by_default():
    messages = [
        {
            "role": "user",
            "content": f"第 {index} 条预算记录",
            "timestamp": f"2026-10-30T10:0{index}:00",
            "metadata": {"_transcript_turn_id": f"t{index}"},
        }
        for index in range(1, 6)
    ]

    first_page = search_messages(messages, keywords=["预算"], limit=2)
    assert [hit["turn_id"] for hit in first_page["hits"]] == ["t5", "t4"]
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 2

    second_page = search_messages(messages, keywords=["预算"], limit=2, offset=2)
    assert [hit["turn_id"] for hit in second_page["hits"]] == ["t3", "t2"]
    assert second_page["has_more"] is True

    oldest_page = search_messages(messages, keywords=["预算"], limit=2, order="oldest")
    assert [hit["turn_id"] for hit in oldest_page["hits"]] == ["t1", "t2"]


def test_open_message_reads_full_message_and_rejects_stale_or_hidden_rows():
    messages = _messages()
    result = search_messages(messages, keywords=["审批"], match="any")
    hit = next(item for item in result["hits"] if item["turn_id"] == "t1")
    opened = open_message(
        messages,
        message_index=hit["message_index"],
        expected_turn_id=hit["turn_id"],
    )
    assert "审批" in opened["content"]
    assert opened["truncated"] is False

    with pytest.raises(ValueError, match="stale_message_index"):
        open_message(messages, message_index=hit["message_index"], expected_turn_id="wrong")

    with pytest.raises(ValueError, match="current_turn_excluded"):
        open_message(messages, message_index=5, current_turn_id="t3")

    with pytest.raises(ValueError, match="message_not_searchable"):
        open_message(messages, message_index=2)


def test_open_message_truncates_long_content_with_remaining_count():
    messages = [{"role": "assistant", "content": "a" * 5000, "turn_id": "long"}]
    opened = open_message(messages, message_index=0, max_chars=300)
    assert opened["truncated"] is True
    assert opened["remaining_chars"] == 4700
    assert len(opened["content"]) == 300


def _load_tool(loop):
    spec = importlib.util.spec_from_file_location(
        "test_search_session_history_tool", TOOL_ROOT / "main" / "tool.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(SimpleNamespace(loop=loop))


def _persisted_session(tmp_path: Path):
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("web:test-session")
    for row in _messages():
        session.add_message(
            str(row["role"]),
            row.get("content", ""),
            **{key: value for key, value in row.items() if key not in {"role", "content"}},
        )
    manager.save(session)
    manager.invalidate("web:test-session")
    return manager


async def test_tool_searches_persisted_session_and_opens_a_hit(tmp_path: Path):
    manager = _persisted_session(tmp_path)
    tool = _load_tool(SimpleNamespace(sessions=manager))
    runtime = {
        "session_key": "web:test-session",
        "loop": SimpleNamespace(sessions=manager),
        "turn_id": "t3",
        "trust_level": "owner",
    }

    search_payload = json.loads(
        await tool.execute(__g3ku_runtime=runtime, keywords=["预算"], limit=5)
    )
    assert search_payload["ok"] is True
    assert search_payload["session_key"] == "web:test-session"
    assert search_payload["total_matches"] == 3

    hit = search_payload["hits"][-1]
    open_payload = json.loads(
        await tool.execute(
            __g3ku_runtime=runtime,
            action="open",
            message_index=hit["message_index"],
            expected_turn_id=hit["turn_id"],
        )
    )
    assert open_payload["ok"] is True
    assert "预算" in open_payload["content"]


async def test_tool_is_owner_only_and_requires_keywords(tmp_path: Path):
    manager = _persisted_session(tmp_path)
    tool = _load_tool(SimpleNamespace(sessions=manager))
    third_party = {
        "session_key": "ext:qq:group",
        "loop": SimpleNamespace(sessions=manager),
        "trust_level": "third_party_group",
    }
    denied = json.loads(await tool.execute(__g3ku_runtime=third_party, keywords=["预算"]))
    assert denied["ok"] is False
    assert denied["error"] == "owner_only"

    owner = {
        "session_key": "web:test-session",
        "loop": SimpleNamespace(sessions=manager),
        "trust_level": "owner",
    }
    missing_keywords = json.loads(await tool.execute(__g3ku_runtime=owner))
    assert missing_keywords["ok"] is False
    assert "keywords is required" in missing_keywords["message"]

    missing_index = json.loads(await tool.execute(__g3ku_runtime=owner, action="open"))
    assert missing_index["ok"] is False
    assert "message_index is required" in missing_index["message"]


def test_manifest_declares_ceo_only_rerunnable_contract():
    manifest = yaml.safe_load((TOOL_ROOT / "resource.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "search_session_history"
    assert manifest["tool_result_delivery_contract"] == "runtime_managed"
    assert manifest["governance"]["allowed_roles"] == ["ceo"]
    assert manifest["recovery_policy"]["rerun_safe"] is True
    assert manifest["exposure"] == {"agent": True, "main_runtime": False}
    assert manifest["toolskill"]["enabled"] is True


def test_tool_is_discoverable_and_buildable_from_the_tools_directory(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    tools_dir = workspace / "tools"
    tools_dir.mkdir()
    shutil.copytree(TOOL_ROOT, tools_dir / "search_session_history")

    registry = ResourceRegistry(
        workspace=workspace,
        skills_dir=workspace / "skills",
        tools_dir=tools_dir,
    )
    descriptor = registry.discover().tools["search_session_history"]
    assert descriptor.available is True
    assert descriptor.toolskill_enabled is True
    assert descriptor.exposure == {"agent": True, "main_runtime": False}

    built = ResourceLoader(workspace).load_tool(
        descriptor, services={"loop": SimpleNamespace(sessions=None)}
    )
    assert built is not None
    assert built.name == "search_session_history"


async def test_discovered_tool_executes_with_runtime_context(tmp_path: Path):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    tools_dir = workspace / "tools"
    tools_dir.mkdir()
    shutil.copytree(TOOL_ROOT, tools_dir / "search_session_history")
    registry = ResourceRegistry(
        workspace=workspace,
        skills_dir=workspace / "skills",
        tools_dir=tools_dir,
    )
    descriptor = registry.discover().tools["search_session_history"]
    manager = _persisted_session(tmp_path / "session-data")
    built = ResourceLoader(workspace).load_tool(
        descriptor, services={"loop": SimpleNamespace(sessions=manager)}
    )
    runtime = {
        "session_key": "web:test-session",
        "loop": SimpleNamespace(sessions=manager),
        "turn_id": "t3",
        "trust_level": "owner",
    }
    payload = json.loads(
        await built.execute(__g3ku_runtime=runtime, keywords=["预算"], limit=1)
    )
    assert payload["ok"] is True
    assert payload["total_matches"] == 3
    assert len(payload["hits"]) == 1
