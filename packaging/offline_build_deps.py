#!/usr/bin/env python3
"""Resolve and fetch the project's build backend for an offline kit.

``uv.lock`` covers the runtime closure but not ``[build-system] requires``, and
``uv sync`` still builds the project itself (PEP 660 editable) - so a kit without
hatchling and its own dependencies dies at "hatchling was not found in the provided
package locations". Measured on the first end-to-end kit run: that was the exact
failure, and it is invisible until something actually installs offline.

The pins come from ``packaging/build-requires.txt`` (regenerate with the command in
``packaging/build-requires.in``). Each pin is resolved through PyPI's JSON API, which
also carries the sha256, so the kit records digests it can verify against.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = REPO_ROOT / "packaging" / "build-requires.txt"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"

# Same platform families as offline_kit_plan.py; kept literal so this module has no
# import edge into the planner.
PLATFORM_FILES = {
    "win-x64": (re.compile(r"win_amd64\.whl$"),),
    "linux-x64": (re.compile(r"(manylinux|musllinux)[\w.]*x86_64\.whl$"),),
    "macos-arm64": (re.compile(r"macosx_\d+_\d+_(arm64|universal2)\.whl$"),),
}


def parse_pins(text: str) -> list[tuple[str, str]]:
    pins: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.fullmatch(r"([A-Za-z0-9._-]+)==([A-Za-z0-9.[-]+)", line.split(" ")[0].split(";")[0])
        if m:
            pins.append((m.group(1), m.group(2)))
    return pins


def choose_file(release: dict, name: str, version: str, platform: str) -> dict:
    """Pick the artifact for this platform: pure wheel, then platform wheel, else fail."""
    files = [f for f in release.get("urls", []) if f.get("packagetype") == "bdist_wheel"]
    pure = [f for f in files if "py3-none-any" in f["filename"] or "py2.py3-none-any" in f["filename"]]
    if pure:
        return pure[0]
    for pattern in PLATFORM_FILES[platform]:
        matched = [f for f in files if pattern.search(f["filename"])]
        if matched:
            return matched[0]
    raise LookupError(
        f"{name}=={version} has no wheel usable on {platform}; a device cannot build it offline"
    )


def resolve(pins: list[tuple[str, str]], platform: str, fetch=urllib.request.urlopen) -> list[dict]:
    entries: list[dict] = []
    for name, version in pins:
        with fetch(PYPI_JSON.format(name=name, version=version), timeout=60) as response:
            release = json.loads(response.read().decode("utf-8"))
        chosen = choose_file(release, name, version, platform)
        digest = (chosen.get("digests") or {}).get("sha256", "")
        if len(digest) != 64:
            raise LookupError(f"{chosen['filename']} has no sha256 from PyPI")
        entries.append(
            {
                "name": name,
                "version": version,
                "filename": chosen["filename"],
                "url": chosen["url"],
                "sha256": digest,
                "size": int(chosen.get("size") or 0),
                "source": "build-backend",
            }
        )
    return entries


def download(entries: list[dict], out_dir: Path) -> list[dict]:
    import hashlib

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict] = []
    for entry in entries:
        target = out_dir / entry["filename"]
        if not target.is_file():
            target.write_bytes(urllib.request.urlopen(entry["url"], timeout=120).read())
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != entry["sha256"]:
            raise SystemExit(f"[kit] {entry['filename']}: PyPI says {entry['sha256']}, file is {actual}")
        written.append({**entry, "size": target.stat().st_size})
    return written


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, choices=sorted(PLATFORM_FILES))
    ap.add_argument("--pins", default=str(PIN_FILE))
    ap.add_argument("--download-to", default="")
    ap.add_argument("--json", dest="json_out", default="",
                    help="write the resolved list here as well as to stdout")
    args = ap.parse_args(argv)
    pins_path = Path(args.pins)
    if not pins_path.is_file():
        print(f"[kit] {pins_path} is missing; regenerate it from packaging/build-requires.in", file=sys.stderr)
        return 1
    entries = resolve(parse_pins(pins_path.read_text(encoding="utf-8")), args.platform)
    if args.download_to:
        entries = download(entries, Path(args.download_to))
    payload = json.dumps({"build_requires": entries}, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(payload, encoding="utf-8")
    if not args.json_out:
        print(payload)
    print("[kit] build backend: " + ", ".join(e["filename"] for e in entries), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
