"""Contract tests for the content path-mode directory copy.

A directory handed to content path mode has to be answered from three places at once —
the `path` parameter description, the tool's toolskill, and the error text itself. Only
one of the three tools carried that wording before, so a model that hydrated the other two
learned nothing until it retried the same directory several times. These assertions fail
if any single carrier is edited alone.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from g3ku.agent.tools.filesystem_stat import FilesystemStatTool
from g3ku.content.navigation import ContentNavigationService

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTENT_TOOLS = ("content_describe", "content_open", "content_search")


def _manifest(tool_id: str) -> dict:
    path = REPO_ROOT / "tools" / tool_id / "resource.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


def _toolskill(tool_id: str) -> str:
    path = REPO_ROOT / "tools" / tool_id / "toolskills" / "SKILL.md"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("tool_id", CONTENT_TOOLS)
def test_path_parameter_names_both_fallback_lanes(tool_id: str) -> None:
    description = _manifest(tool_id)["parameters"]["properties"]["path"]["description"]
    lowered = description.lower()
    assert "directory" in lowered, description
    assert "filesystem_stat" in lowered, description
    assert "exec" in lowered, description


@pytest.mark.parametrize("tool_id", CONTENT_TOOLS)
def test_toolskill_states_the_directory_rule(tool_id: str) -> None:
    text = _toolskill(tool_id).lower()
    assert "filesystem_stat" in text, tool_id
    assert "directory" in text, tool_id


def test_combined_target_results_are_documented() -> None:
    for tool_id in ("content_open", "content_search"):
        text = _toolskill(tool_id)
        assert "targets.ref" in text and "targets.path" in text, tool_id


def test_stat_description_declares_it_does_not_search_contents() -> None:
    assert "does not search file contents" in FilesystemStatTool().model_description


def test_directory_error_offers_a_lane_on_every_path_mode_entry() -> None:
    root = Path(tempfile.mkdtemp())
    directory = root / "sub"
    directory.mkdir()
    service = ContentNavigationService(workspace=root)

    with pytest.raises(ValueError) as descriptor_error:
        service.open_target_descriptor(path=str(directory))
    with pytest.raises(ValueError) as search_error:
        service.search(query="needle", path=str(directory))
    with pytest.raises(ValueError) as ref_error:
        service.search(query="needle", ref=f"path:{directory.name}")

    for error in (descriptor_error, search_error, ref_error):
        message = str(error.value)
        assert message.startswith("path is not a file: ")
        assert "filesystem_stat" in message
        assert "exec" in message
