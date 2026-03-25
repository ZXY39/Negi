from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[2] / 'skills' / 'clawhub-skill-manager' / 'scripts' / 'clawhub_skill_manager.py'
    spec = importlib.util.spec_from_file_location('clawhub_skill_manager', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_split_frontmatter_and_first_paragraph() -> None:
    module = _load_module()
    text = (
        '---\n'
        'name: demo-skill\n'
        'description: Demo summary\n'
        '---\n\n'
        '# Demo\n\n'
        'This is the first paragraph.\n\n'
        '```\nignored\n```\n'
    )

    frontmatter, body = module.split_frontmatter(text)

    assert frontmatter['name'] == 'demo-skill'
    assert frontmatter['description'] == 'Demo summary'
    assert module.first_paragraph(body) == 'This is the first paragraph.'


def test_build_resource_manifest_marks_clawhub_source(tmp_path) -> None:
    module = _load_module()
    skill_root = tmp_path / 'demo-skill'
    skill_root.mkdir()
    (skill_root / 'references').mkdir()
    (skill_root / 'SKILL.md').write_text(
        '---\nname: demo-skill\ndescription: Demo summary\n---\n\n# Demo\n',
        encoding='utf-8',
    )

    manifest = module.build_resource_manifest(
        skill_root=skill_root,
        skill_id='demo-skill',
        slug='demo-skill',
        detail={
            'skill': {'slug': 'demo-skill', 'displayName': 'Demo Skill', 'summary': 'Remote summary'},
            'owner': {'handle': 'alice', 'displayName': 'Alice'},
            'latestVersion': {'version': '1.2.3', 'createdAt': 1_700_000_000_000},
        },
        version_payload={'version': {'version': '1.2.3', 'createdAt': 1_700_000_000_000}},
        upstream_meta={'slug': 'demo-skill', 'version': '1.2.3', 'publishedAt': 1_700_000_000_000},
    )

    assert manifest['kind'] == 'skill'
    assert manifest['name'] == 'demo-skill'
    assert manifest['source']['type'] == 'clawhub'
    assert manifest['source']['slug'] == 'demo-skill'
    assert manifest['current_version']['version'] == '1.2.3'
    assert manifest['content']['main'] == 'SKILL.md'
    assert manifest['content']['references'] == 'references'
