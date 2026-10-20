"""阶段 1：前门钉住整表的落盘载体与闸门放行。

钉住的合同是"成员只在重印边界变"。今天删掉一颗工具之后，重启那一跳会**每跳少一条**：
可渲染性闸门按"活实例 ∪ 删除前快照"摘名，而那两份都是进程内的、重启即空——被摘的名字
还留在 durable 钉住名单里，于是断点每跳重付一次。阶段 1 把整表在重写那一刻落成会话级
singleton 文件，并把闸门判据放宽到"整表里供得出字节也算可渲染"。读路径本阶段不动。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.resources import retired_schemas
from g3ku.runtime.frontdoor import pinned_tool_bundle
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner


class _SchemaTool:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description
        self.parameters: dict = {}
        self._payload = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }

    def to_model_schema(self) -> dict:
        return dict(self._payload)


def _state(session_key: str, names: list[str]) -> dict:
    return {
        "provider_tool_names": list(names),
        "session_key": session_key,
        "hydrated_tool_names": [],
        "rbac_visible_tool_names": [],
        "messages": [],
        "frontdoor_stage_state": {},
    }


def _runner(tool_names: list[str]) -> CreateAgentCeoFrontDoorRunner:
    tools = {name: _SchemaTool(name, f"{name} schema") for name in tool_names}
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(tools=dict(tools)))
    runner._frontdoor_execution_bundle = lambda **kwargs: SimpleNamespace(
        declared_tools={name: tools[name] for name in tool_names if name in tools}
    )
    return runner


def _session(session_key: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(session_key=session_key))


def _render(runner, session, session_key: str, names: list[str]) -> list[dict]:
    return runner._frontdoor_tool_schemas_for_state(
        state=_state(session_key, names),
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )


@pytest.fixture(autouse=True)
def isolated_bundle_store(tmp_path, monkeypatch):
    """把整表载体钉在 tmp 数据根上，并清掉两份模块级状态。

    进程表与 `retired_schemas` 都是模块级的：不清的话上一条用例留下的整表会让这一条
    "看起来恢复了"，实际读的是上一轮的缓存。
    """
    monkeypatch.setattr(pinned_tool_bundle, "data_root", lambda *args, **kwargs: tmp_path)
    pinned_tool_bundle._entries.clear()
    retired_schemas.clear_tool_schemas()
    yield tmp_path
    pinned_tool_bundle._entries.clear()
    retired_schemas.clear_tool_schemas()


def test_renderer_persists_bundle_and_records_path_and_revision(tmp_path) -> None:
    session_key = "web:persist"
    session = _session(session_key)

    _render(_runner(["alpha", "beta"]), session, session_key, ["alpha", "beta"])

    assert getattr(session, "_frontdoor_pinned_tool_bundle_fault", 0) == 0
    path = Path(str(session._frontdoor_pinned_tool_bundle_path))
    revision = str(session._frontdoor_pinned_tool_bundle_revision)
    assert path.parent == (tmp_path / pinned_tool_bundle.PINNED_TOOL_BUNDLE_ROOT)
    assert path.exists() and revision
    payload = json.loads(path.read_text(encoding="utf-8"))
    # names 是钉住顺序，含渲染器追加的两颗合成控制工具；tool_schemas 才是线上字节。
    assert payload["names"] == ["alpha", "beta", "submit_next_stage", "silent"]
    assert [str((item.get("function") or {}).get("name")) for item in payload["tool_schemas"]] == payload["names"]
    assert payload["build_reason"] == "membership_changed"


def test_singleton_path_is_reused_while_revision_tracks_content() -> None:
    """F2 那条坑：就地覆盖复用同一路径 ⇒ 只按路径缓存会供旧字节。

    revision 必须随内容变、路径不变，`(path, revision)` 才当得了缓存键。
    """
    session_key = "web:singleton"
    session = _session(session_key)

    _render(_runner(["alpha"]), session, session_key, ["alpha"])
    first_path = str(session._frontdoor_pinned_tool_bundle_path)
    first_revision = str(session._frontdoor_pinned_tool_bundle_revision)

    _render(_runner(["alpha", "beta"]), session, session_key, ["alpha", "beta"])
    assert str(session._frontdoor_pinned_tool_bundle_path) == first_path
    assert str(session._frontdoor_pinned_tool_bundle_revision) != first_revision
    # 旧 revision 不再能从文件里读到（文件已被就地覆盖）：按 (path, 旧 revision) 取索引
    # 必须落到"读不到"，不能返回新内容冒充旧的。
    assert pinned_tool_bundle.bundle_names_for_session(session_key) == [
        "alpha",
        "beta",
        "submit_next_stage",
        "silent",
    ]


def test_bundle_names_include_the_two_synthetic_control_tools() -> None:
    """N5：前门的合成控制工具不在治理集也不在注册表，靠渲染器追加。

    整表若不显式含它们，阶段 2 "按名单从整表取字节"会静默少两名——模型看不见阶段工具，
    那是功能回归不是缓存事件。
    """
    session_key = "web:control-tools"
    session = _session(session_key)

    _render(_runner(["alpha"]), session, session_key, ["alpha"])

    names = pinned_tool_bundle.bundle_names_for_session(session_key)
    assert names[-2:] == ["submit_next_stage", "silent"]
    assert set(session._frontdoor_pinned_tool_schema_bundle) >= {"submit_next_stage", "silent"}


def test_deleted_tool_survives_restart_byte_for_byte() -> None:
    """红绿翻转：删一颗工具 → 进程载体清空（等价重启）→ 恢复 → 名单与字节都不许变。

    今天这条必然红：重启后活实例与删除前快照都空，闸门把名字摘掉，wire 少一条。
    """
    session_key = "web:restart-pin"
    runner = _runner(["alpha", "beta"])
    session = _session(session_key)
    before = _render(runner, session, session_key, ["alpha", "beta"])
    path = str(session._frontdoor_pinned_tool_bundle_path)
    revision = str(session._frontdoor_pinned_tool_bundle_revision)

    # 资源被真删：注册表里没有实例，也没留下删除前快照（外部改名走的就是这条路）。
    runner._loop.tools.pop("beta", None)
    assert retired_schemas.lookup_tool_schema("beta") is None

    pinned_tool_bundle._entries.clear()
    restored_session = _session(session_key)
    assert (
        pinned_tool_bundle.restore_pinned_tool_bundle_state(
            restored_session,
            session_key=session_key,
            path=path,
            revision=revision,
        )
        is True
    )

    after = _render(runner, restored_session, session_key, ["alpha", "beta"])
    assert after == before
    assert "beta" not in runner._loop.tools


def test_gate_keeps_bundle_backed_name_and_still_drops_unknown_name() -> None:
    """Q-A：闸门放行只放开"整表供得出字节"的那一档，未知名字照旧不进。"""
    session_key = "web:gate"
    session = _session(session_key)
    runner = _runner(["alpha"])
    _render(runner, session, session_key, ["alpha", "vanished"])

    kept = runner._frontdoor_schema_renderable_tool_names(
        ["alpha", "vanished", "never_seen"],
        session_key=session_key,
        session=session,
    )
    assert "vanished" in kept
    assert "never_seen" not in kept

    # 换个没有整表载体的会话：放行不能凭空把名字留下，否则闸门退化成"名单原样通过"。
    other_runner = _runner(["alpha"])
    assert "vanished" not in other_runner._frontdoor_schema_renderable_tool_names(
        ["alpha", "vanished"],
        session_key="web:other-session",
    )


def test_restore_does_not_overwrite_a_fresh_carrier() -> None:
    """恢复纪律：载体非空时不覆盖，否则刚重印的那一遍会被旧文件抹掉。"""
    session_key = "web:discipline"
    session = _session(session_key)
    _render(_runner(["alpha", "beta"]), session, session_key, ["alpha", "beta"])
    path = str(session._frontdoor_pinned_tool_bundle_path)
    revision = str(session._frontdoor_pinned_tool_bundle_revision)

    pinned_tool_bundle._entries.clear()
    session._frontdoor_pinned_tool_schema_bundle = {"alpha": {"type": "function", "function": {"name": "alpha"}}}
    assert (
        pinned_tool_bundle.restore_pinned_tool_bundle_state(
            session,
            session_key=session_key,
            path=path,
            revision=revision,
        )
        is False
    )
    assert set(session._frontdoor_pinned_tool_schema_bundle) == {"alpha"}


def test_broken_bundle_reads_as_missing(tmp_path) -> None:
    """G5：0 字节 / 缺文件 / 空路径一律是"读不到"，不能折叠成"名单为空"。

    满盘写坏过的载体就是 0 字节；读成空索引会让调用方以为"这跳没有兜底源"，而真实语义
    是"这份工件坏了，按故障路径重印"。
    """
    broken = tmp_path / "broken.json"
    broken.write_text("", encoding="utf-8")
    assert pinned_tool_bundle.read_pinned_tool_bundle_index(session_key="web:broken", path=str(broken)) == {}
    assert pinned_tool_bundle.read_pinned_tool_bundle_index(session_key="web:broken", path="") == {}
    assert (
        pinned_tool_bundle.read_pinned_tool_bundle_index(
            session_key="web:broken", path=str(tmp_path / "not-here.json")
        )
        == {}
    )


def test_session_deletion_drops_the_bundle_file() -> None:
    session_key = "web:forget"
    session = _session(session_key)
    _render(_runner(["alpha"]), session, session_key, ["alpha"])
    path = Path(str(session._frontdoor_pinned_tool_bundle_path))
    assert path.exists()

    pinned_tool_bundle.forget_pinned_tool_bundle(session_key)

    assert not path.exists()
    assert pinned_tool_bundle.bundle_names_for_session(session_key) == []
