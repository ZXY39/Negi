from __future__ import annotations

import importlib.util
import io
import os
import zipfile
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / 'skills'
    / 'clawhub-skill-manager'
    / 'scripts'
    / 'clawhub_skill_manager.py'
)


def _load_module():
    # Directory name ('clawhub-skill-manager') contains a hyphen so the script
    # cannot be imported by name; load it by path instead.
    spec = importlib.util.spec_from_file_location('clawhub_skill_manager', MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_archive(tmp_path: Path, members: dict[str, bytes]) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    archive_path = tmp_path / 'archive.zip'
    archive_path.write_bytes(buf.getvalue())
    return archive_path


def _no_files(directory: Path) -> None:
    assert directory.exists()
    assert [p.name for p in directory.iterdir()] == []


def test_zip_slip_dotdot_escape_is_rejected_without_partial_extraction(tmp_path) -> None:
    module = _load_module()
    archive = _write_archive(
        tmp_path,
        {'good.txt': b'ok', '../escaped.txt': b'evil', '../escaped2.txt': b'evil2'},
    )
    staging = tmp_path / 'staging'

    with pytest.raises(ValueError) as excinfo:
        module.unpack_archive(archive, staging)

    message = str(excinfo.value)
    assert 'escaped.txt' in message
    assert 'escaped2.txt' in message
    # Nothing landed outside the staging directory...
    assert not (tmp_path / 'escaped.txt').exists()
    assert not (tmp_path / 'escaped2.txt').exists()
    # ...and no partial extraction: not even the benign member was unpacked.
    _no_files(staging)


def test_zip_slip_absolute_path_member_is_rejected(tmp_path) -> None:
    module = _load_module()
    target = tmp_path / 'absolute_escape.txt'
    member_name = os.path.join(str(tmp_path), 'absolute_escape.txt')
    archive = _write_archive(tmp_path, {member_name: b'evil'})
    staging = tmp_path / 'staging'

    with pytest.raises(ValueError) as excinfo:
        module.unpack_archive(archive, staging)

    assert target.name in str(excinfo.value)
    assert not target.exists()
    _no_files(staging)


def test_zip_slip_normal_archive_with_top_level_dir_extracts_unchanged(tmp_path) -> None:
    module = _load_module()
    archive = _write_archive(
        tmp_path,
        {
            'demo-skill/SKILL.md': b'# Demo\n',
            'demo-skill/references/guide.md': b'Guide\n',
        },
    )
    staging = tmp_path / 'staging'

    extracted_root = module.unpack_archive(archive, staging)

    assert extracted_root == staging / 'demo-skill'
    assert (staging / 'demo-skill' / 'SKILL.md').read_bytes() == b'# Demo\n'
    assert (staging / 'demo-skill' / 'references' / 'guide.md').read_bytes() == b'Guide\n'