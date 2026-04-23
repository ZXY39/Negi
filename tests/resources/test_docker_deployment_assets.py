from __future__ import annotations

from pathlib import Path

import yaml


def test_compose_persists_runtime_and_resource_dirs() -> None:
    payload = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    web = dict(payload["services"]["web"])
    worker = dict(payload["services"]["worker"])
    mounts = "\n".join(str(item) for item in list(web.get("volumes") or []) + list(worker.get("volumes") or []))

    for required in (".g3ku", "memory", "sessions", "temp", "skills", "tools", "externaltools"):
        assert required in mounts

    assert "web-entrypoint.sh" in str(web.get("command"))
    assert "/api/bootstrap/status" in str(web.get("healthcheck"))


def test_dockerfile_seeds_mutable_resources_and_uses_uv() -> None:
    text = Path("Dockerfile").read_text(encoding="utf-8")
    script = Path("docker/web-entrypoint.sh").read_text(encoding="utf-8")

    assert "uv sync --frozen" in text
    assert "pnpm install --frozen-lockfile" in text
    assert "/opt/g3ku-seed/skills" in text
    assert "/opt/g3ku-seed/tools" in text
    assert "--no-worker" in script


def test_dockerfile_pins_utf8_locale_and_python_stdio_encoding() -> None:
    text = Path("Dockerfile").read_text(encoding="utf-8")

    assert "LANG=C.UTF-8" in text
    assert "LC_ALL=C.UTF-8" in text
    assert "PYTHONIOENCODING=utf-8" in text
