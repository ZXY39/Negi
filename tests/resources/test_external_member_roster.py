from __future__ import annotations

from g3ku.runtime import external_member_roster


def test_roster_assigns_stable_numbers_and_resolves_unique_names(tmp_path, monkeypatch):
    monkeypatch.setattr(
        external_member_roster,
        "data_work_path",
        lambda *parts: tmp_path.joinpath(*parts),
    )
    roster = external_member_roster.MemberRoster("ext:test:group:1")
    members = roster.update_many(
        [
            {"id": "AAAA", "name": "Alice", "role": "admin"},
            {"id": "BBBB", "name": "Bob", "role": "member"},
        ]
    )
    assert members["AAAA"]["number"] == 2
    assert members["BBBB"]["number"] == 3
    assert roster.resolve("Alice")["id"] == "AAAA"
    assert roster.resolve("0003")["id"] == "BBBB"
    assert roster.resolve("Alice2")["reason"] == "target_not_found"


def test_roster_name_ambiguity_returns_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(
        external_member_roster,
        "data_work_path",
        lambda *parts: tmp_path.joinpath(*parts),
    )
    roster = external_member_roster.MemberRoster("ext:test:group:2")
    roster.update_many(
        [
            {"id": "AAAA", "name": "同名"},
            {"id": "BBBB", "name": "同名"},
        ]
    )
    result = roster.resolve("同名")
    assert result["ok"] is False
    assert result["reason"] == "ambiguous_name"
    assert len(result["candidates"]) == 2


def test_roster_master_is_resolved_by_flag_not_nickname(tmp_path, monkeypatch):
    monkeypatch.setattr(
        external_member_roster,
        "data_work_path",
        lambda *parts: tmp_path.joinpath(*parts),
    )
    roster = external_member_roster.MemberRoster("ext:test:group:3")
    roster.update_many(
        [
            {"id": "AAAA", "name": "Master", "master": False},
            {"id": "OWNER", "name": "操作员", "master": True},
        ]
    )
    assert roster.resolve("master")["id"] == "OWNER"
    assert roster.resolve("1")["id"] == "OWNER"
    # “Master” 是保留目标，不能被同名普通成员占用。
    assert roster.resolve("Master")["id"] == "OWNER"
