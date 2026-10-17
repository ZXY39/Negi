"""一个坏清单只能杀掉它自己，不能杀掉整次资源发现。

`ResourceRegistry._safe_manifest` 的降级道只捕 `ResourceManifestError`，而 `load_manifest`
里 `int(schema_version)` 对非数字值抛的是裸 `ValueError`/`TypeError`——一条写坏的
`resource.yaml` 因此会让 skills + tools 全量发现抛异常，工具运行时整个空掉。
"""

from __future__ import annotations

import pytest

from g3ku.resources.manifest import ResourceManifestError, load_manifest
from g3ku.resources.registry import ResourceRegistry

BAD_MANIFEST = """
schema_version: v1
kind: skill
name: broken-skill
description: "version field is not a number"
"""

GOOD_SKILL = """
schema_version: 1
kind: skill
name: healthy-skill
description: "loads fine"
"""


def _workspace(tmp_path):
    skills = tmp_path / "skills"
    broken = skills / "broken-skill"
    healthy = skills / "healthy-skill"
    for directory in (broken, healthy):
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("# body\n", encoding="utf-8")
    (broken / "resource.yaml").write_text(BAD_MANIFEST, encoding="utf-8")
    (healthy / "resource.yaml").write_text(GOOD_SKILL, encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    return tmp_path, skills, tools


def test_non_numeric_schema_version_raises_the_coded_error(tmp_path) -> None:
    workspace, skills, _tools = _workspace(tmp_path)
    path = skills / "broken-skill" / "resource.yaml"

    with pytest.raises(ResourceManifestError) as raised:
        load_manifest(path)

    assert "schema_version" in str(raised.value)


def test_discovery_survives_one_bad_manifest(tmp_path) -> None:
    workspace, _skills, tools = _workspace(tmp_path)
    registry = ResourceRegistry(workspace, skills_dir=workspace / "skills", tools_dir=tools)

    snapshot = registry.discover()

    assert "healthy-skill" in snapshot.skills
    assert "broken-skill" not in snapshot.skills or snapshot.skills["broken-skill"].description == ""
