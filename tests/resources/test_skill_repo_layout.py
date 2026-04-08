from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_skills_are_not_tracked_as_gitlinks():
    if not (REPO_ROOT / '.git').exists():
        pytest.skip('requires a git checkout')

    completed = subprocess.run(
        ['git', 'ls-files', '--stage', '--', 'skills'],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    gitlinks = []
    for line in completed.stdout.splitlines():
        fields = line.split(maxsplit=3)
        if fields and fields[0] == '160000':
            gitlinks.append(fields[-1])

    assert not gitlinks, f'skills/ contains gitlink entries instead of tracked files: {gitlinks}'
