"""filesystem_stat：只读测量工具契约。

验收节点曾因缺少测量通道，把内容工具对二进制文件返回的占位串统计当成"实测文件大小"，
把 629KB 的合法 PDF 判成 34 字节空壳（2026-09-17 task:77d0ae460cf0）。本工具提供真实
体积、mtime 与目录清单/聚合，且不含任何写操作。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from g3ku.agent.tools.filesystem_stat import FilesystemStatTool

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_tool(tmp_path: Path, *, restrict: bool = False) -> FilesystemStatTool:
    return FilesystemStatTool(
        workspace=tmp_path,
        allowed_dir=tmp_path if restrict else None,
    )


@pytest.mark.asyncio
async def test_measures_real_file_size_and_mtime(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    target = tmp_path / "resume.pdf"
    target.write_bytes(b"%PDF-1.4\n" + b"x" * 1234)

    result = await tool.execute(paths=[str(target)])

    item = result["items"][0]
    assert item["exists"] is True
    assert item["kind"] == "file"
    assert item["size_bytes"] == target.stat().st_size
    assert item["suffix"] == ".pdf"
    assert item["mtime"]


@pytest.mark.asyncio
async def test_directory_aggregates_cover_whole_tree(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "small.txt").write_bytes(b"a" * 10)
    (tmp_path / "big.pdf").write_bytes(b"b" * 5000)
    (tmp_path / "sub" / "nested.md").write_bytes(b"c" * 100)

    result = await tool.execute(paths=[str(tmp_path)])

    item = result["items"][0]
    assert item["kind"] == "directory"
    assert item["file_count"] == 3
    assert item["dir_count"] == 1
    assert item["total_bytes"] == 5110
    assert item["largest_file"]["name"] == "big.pdf"
    assert item["smallest_file"]["name"] == "small.txt"
    assert {entry["name"] for entry in item["entries"]} == {"sub", "small.txt", "big.pdf"}


@pytest.mark.asyncio
async def test_missing_path_is_reported_without_size_claims(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    missing = tmp_path / "data-005.pdf"

    result = await tool.execute(paths=[str(missing)])

    item = result["items"][0]
    assert item["exists"] is False
    assert "size_bytes" not in item
    assert result["missing"] == [str(missing)]


@pytest.mark.asyncio
async def test_entries_are_capped_but_aggregates_are_not(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    for index in range(25):
        (tmp_path / f"f-{index:03d}.pdf").write_bytes(b"x")

    result = await tool.execute(paths=[str(tmp_path)], max_entries=5)

    item = result["items"][0]
    assert len(item["entries"]) == 5
    assert item["file_count"] == 25
    assert item["entries_truncated"] is True


@pytest.mark.asyncio
async def test_restrict_to_workspace_rejects_outside_paths(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path, restrict=True)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    result = await tool.execute(paths=[str(outside)])

    item = result["items"][0]
    assert item["exists"] is False
    assert "outside allowed directory" in str(item.get("error") or "")


@pytest.mark.asyncio
async def test_measurement_does_not_modify_targets(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    target = tmp_path / "keep.pdf"
    target.write_bytes(b"%PDF-1.4\n" + b"y" * 100)
    before = (target.stat().st_size, target.stat().st_mtime_ns)

    await tool.execute(paths=[str(tmp_path), str(target)])

    after = (target.stat().st_size, target.stat().st_mtime_ns)
    assert before == after
    assert sorted(path.name for path in tmp_path.iterdir()) == ["keep.pdf"]


def test_manifest_grants_inspection_role_measurement_action() -> None:
    manifest = (REPO_ROOT / "tools/filesystem_stat/resource.yaml").read_text(encoding="utf-8")
    assert "family: filesystem" in manifest
    assert "id: stat" in manifest
    assert "restrict_to_workspace: false" in manifest
    # inspection 必须能测量：没有这条授权，验收节点就拿不到真实体积。
    assert manifest.count("inspection") >= 1
    assert (REPO_ROOT / "tools/filesystem_stat/toolskills/SKILL.md").is_file()


class _StubFamilyRegistry:
    def __init__(self, families):
        self._families = list(families)

    def list_tool_families(self):
        return list(self._families)

    def list_skill_resources(self):
        return []

    def get_tool_family(self, tool_id: str):
        for family in self._families:
            if str(getattr(family, "tool_id", "")) == str(tool_id):
                return family
        return None


def _discover_filesystem_families(tmp_path: Path):
    import shutil

    from g3ku.resources.registry import ResourceRegistry
    from main.governance.resource_bridge import build_tool_families

    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    (workspace / "tools").mkdir(parents=True, exist_ok=True)
    for tool_name in ("filesystem_stat", "filesystem_write"):
        shutil.copytree(REPO_ROOT / "tools" / tool_name, workspace / "tools" / tool_name)
    registry = ResourceRegistry(workspace, skills_dir=workspace / "skills", tools_dir=workspace / "tools")
    snapshot = registry.discover()
    return {family.tool_id: family for family in build_tool_families(list(snapshot.tools.values()))}


def test_stat_action_joins_filesystem_family_with_inspection_role(tmp_path: Path) -> None:
    family = _discover_filesystem_families(tmp_path)["filesystem"]
    action_map = {action.action_id: action for action in family.actions}

    stat_action = action_map["stat"]
    assert "filesystem_stat" in stat_action.executor_names
    assert "inspection" in stat_action.allowed_roles
    assert stat_action.destructive is False
    # 写动作对 inspection 的拒绝不受影响：验收仍是只读裁判。
    assert "inspection" not in action_map["write"].allowed_roles


def test_inspection_role_resolves_allow_for_stat_and_deny_for_write(tmp_path: Path) -> None:
    from main.governance.models import PermissionSubject
    from main.governance.policy_engine import MainRuntimePolicyEngine
    from main.governance.store import GovernanceStore

    families = _discover_filesystem_families(tmp_path)
    store = GovernanceStore(tmp_path / "governance.sqlite3")
    try:
        engine = MainRuntimePolicyEngine(store=store, resource_registry=_StubFamilyRegistry(families.values()))
        engine.sync_default_role_policies()
        subject = PermissionSubject(
            user_key="inspection",
            session_id="web:shared",
            actor_role="inspection",
        )

        allowed = engine.evaluate_tool_action(subject=subject, tool_id="filesystem", action_id="stat")
        denied = engine.evaluate_tool_action(subject=subject, tool_id="filesystem", action_id="write")

        assert allowed.allowed is True
        assert denied.allowed is False
    finally:
        store.close()
