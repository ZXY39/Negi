"""Docker 构建上下文必须排除操作员自己造的 .env。

README「Docker 部署」第 1 步就是"复制 `.env.docker.example` 为 `.env`"，里面放
`G3KU_BOOTSTRAP_PASSWORD` / `G3KU_BOOTSTRAP_MASTER_KEY`；compose 是**运行时**读 `.env`
做变量插值，镜像里根本不需要它。而 `Dockerfile` 用 `COPY . /app` 一次带走整个上下文，
所以 `.dockerignore` 少这一行就等于把口令烤进镜像层（跟着 push 走出去，删不掉）。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ignore_lines() -> list[str]:
    text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def test_operator_env_files_are_out_of_the_build_context() -> None:
    lines = _ignore_lines()
    assert ".env" in lines, lines
    assert ".env.local" in lines, lines


def test_dockerfile_still_copies_the_app_tree() -> None:
    """反向钉：不是靠删掉 COPY 过关的。"""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY . /app" in dockerfile


def test_readme_env_instruction_matches_the_exclusion() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "`.env.docker.example` 为 `.env`" in readme
    assert ".env" in _ignore_lines()
