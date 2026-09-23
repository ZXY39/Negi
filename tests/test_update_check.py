from pathlib import Path

from g3ku.update_check import fetch_latest_release_tag, latest_release_tag, parse_version


def test_parse_version_accepts_bare_and_prefixed():
    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version(" v1.2.10 ") == (1, 2, 10)
    assert parse_version("1.2") is None
    assert parse_version("nightly") is None


def test_latest_release_tag_picks_highest_not_newest_line():
    output = "\n".join(
        [
            "aaa111\trefs/tags/v1.0.0",
            "bbb222\trefs/tags/v1.10.0",
            "ccc333\trefs/tags/v1.9.0^{}",
            "ddd444\trefs/tags/v1.2.0",
        ]
    )
    assert latest_release_tag(output) == "v1.10.0"


def test_latest_release_tag_ignores_non_release_refs():
    output = "\n".join(
        [
            "aaa111\trefs/tags/backup/pre-reword-ac670b5b",
            "bbb222\trefs/tags/v1.0.0^{}",
            "ccc333\trefs/heads/main",
            "ddd444\trefs/tags/v2beta",
        ]
    )
    assert latest_release_tag(output) is None


def test_latest_release_tag_handles_empty_output():
    assert latest_release_tag("") is None


def test_fetch_returns_none_without_origin(tmp_path: Path):
    assert fetch_latest_release_tag(tmp_path, timeout=10.0) is None
