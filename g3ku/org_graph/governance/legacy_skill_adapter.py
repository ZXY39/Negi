from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from g3ku.org_graph.governance.models import SkillResourceRecord
from g3ku.org_graph.public_roles import to_public_allowed_roles

ALL_ROLES = ['ceo', 'execution', 'inspection']


def discover_local_skills(
    skills_dir: Path,
    *,
    default_risk_level: str,
    exclude_names: set[str] | None = None,
) -> list[SkillResourceRecord]:
    excluded = set(exclude_names or set())
    items: list[SkillResourceRecord] = []
    root = Path(skills_dir)
    if not root.exists():
        return items
    for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest_path = skill_dir / 'skill.yaml'
        skill_doc_path = skill_dir / 'SKILL.md'
        if not manifest_path.exists() and not skill_doc_path.exists():
            continue
        manifest = _read_yaml(manifest_path) if manifest_path.exists() else {}
        frontmatter = _read_skill_frontmatter(skill_doc_path) if skill_doc_path.exists() else {}
        skill_id = str(manifest.get('name') or frontmatter.get('name') or skill_dir.name).strip()
        if not skill_id or skill_id in excluded:
            continue
        governance = dict(manifest.get('governance') or {})
        allowed_roles = to_public_allowed_roles([str(role) for role in (governance.get('allowed_roles') or ALL_ROLES)])
        editable_files = [str(item) for item in (governance.get('editable_files') or [])]
        if not editable_files:
            editable_files = ['SKILL.md']
            if manifest_path.exists():
                editable_files.append('skill.yaml')
        openai_yaml_path = skill_dir / 'agents' / 'openai.yaml'
        if openai_yaml_path.exists() and 'agents/openai.yaml' not in editable_files:
            editable_files.append('agents/openai.yaml')
        runtime = dict(manifest.get('runtime') or {})
        items.append(
            SkillResourceRecord(
                skill_id=skill_id,
                capability_name=None,
                display_name=str(manifest.get('display_name') or skill_id),
                description=str(manifest.get('description') or frontmatter.get('description') or '').strip(),
                version=str(manifest.get('version') or '').strip() or None,
                legacy=not manifest_path.exists(),
                enabled=bool(governance.get('enabled_by_default', True)),
                available=skill_doc_path.exists(),
                allowed_roles=allowed_roles,
                editable_files=editable_files,
                risk_level=str(governance.get('risk_level') or default_risk_level),
                requires_tools=[str(item) for item in (runtime.get('requires_tools') or [])],
                source_path=str(skill_dir),
                manifest_path=str(manifest_path) if manifest_path.exists() else None,
                skill_doc_path=str(skill_doc_path),
                openai_yaml_path=str(openai_yaml_path) if openai_yaml_path.exists() else None,
                metadata={
                    'content': dict(manifest.get('content') or {}),
                    'runtime': runtime,
                    'governance': governance,
                    'frontmatter': frontmatter,
                },
            )
        )
    return items


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        return dict(yaml.safe_load(path.read_text(encoding='utf-8')) or {})
    except Exception:
        return {}


def _read_skill_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding='utf-8')
    except Exception:
        return {}
    if not text.startswith('---'):
        return {}
    parts = text.split('---', 2)
    if len(parts) < 3:
        return {}
    try:
        return dict(yaml.safe_load(parts[1]) or {})
    except Exception:
        return {}

