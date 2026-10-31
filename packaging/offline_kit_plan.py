#!/usr/bin/env python3
"""Turn ``uv.lock`` into the artifact list an offline kit needs for one platform.

The lock records, for every package, each wheel's URL, sha256 and byte size per
platform tag. So a kit can be planned - and built - from any runner without
installing anything for the target: read the plan, download, verify against the
lock, done. Packages that ship no compatible wheel are reported as ``prebuild``
(the builder runs ``uv build`` on their sdist, which needs the index - fine in the
build job, never on the device).

Matching is real wheel-tag arithmetic against one target interpreter: a kit for
CPython 3.12 on win-amd64 must not carry ``cp314`` or ``win32`` or ``win_arm64``
builds. Measured on this repo, filename-substring guessing inflated one 33 MB win
kit to 263 MB across 271 files.

Usage:
    python packaging/offline_kit_plan.py --platform win-x64 [--json out.json]

Exit status is non-zero when a needed package has no artifact the kit can carry,
because that kit would only fail once it reaches a device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK = REPO_ROOT / "uv.lock"
PROJECT_NAME = "g3ku-ai"
PYTHON_VERSION = (3, 12)  # .python-version; a kit is per interpreter minor version

# os/arch families for the wheel platform tag, plus the "any" tag every platform takes.
# Patterns, not enumerations: cryptography's only mac wheel is built for
# macosx_10_9_universal2, and listing releases is how it fell out of the mac plan and
# into "build it on the device" - which offline cannot do.
PLATFORMS: dict[str, dict] = {
    "win-x64": {"sys_platform": "win32", "os_name": "nt", "platform_machine": "AMD64",
                "platform_patterns": (r"^win_amd64$",)},
    "linux-x64": {"sys_platform": "linux", "os_name": "posix", "platform_machine": "x86_64",
                  "platform_patterns": (r"^manylinux\d*_\d+_\d+_x86_64$",
                                        r"^manylinux\d+x86_64$",
                                        r"^musllinux_\d+_\d+_x86_64$",
                                        r"^linux_x86_64$")},
    "macos-arm64": {"sys_platform": "darwin", "os_name": "posix", "platform_machine": "arm64",
                    "platform_patterns": (r"^macosx_\d+_\d+_(arm64|universal2)$",)},
}

_WHEEL_NAME = re.compile(
    r"^(?P<name>[^\s-]+(?:_[^\s-]+)*)-(?P<ver>[^\s-]+?)"
    r"(?:-(?P<build>\d+[^\s-]*))?-(?P<py>[^\s-]+)-(?P<abi>[^\s-]+)-(?P<plat>[^\s-]+)\.whl$"
)


def _cpython(tag: str) -> tuple[int, int] | None:
    """``cp37`` is Python 3.7 and ``cp312`` is 3.12: the minor is 1 or 2 digits."""
    m = re.fullmatch(r"cp(\d+)(\d\d|\d)(t?)", tag)
    if not m or m.group(3):
        return None  # free-threaded builds (cp313t) are not this kit's interpreter
    digits = m.group(1) + m.group(2)
    return (int(digits[0]), int(digits[1:]))


def wheel_matches(filename: str, platform: str, python_version: tuple[int, int]) -> bool:
    """Is this wheel installable for the target interpreter and machine?"""
    m = _WHEEL_NAME.match(filename)
    if not m:
        return False
    conf = PLATFORMS[platform]
    plat_tags = m.group("plat").split(".")
    if "any" not in plat_tags and not any(
        re.match(pattern, tag) for tag in plat_tags for pattern in conf["platform_patterns"]
    ):
        return False
    py_tags = m.group("py").split(".")
    abi_tags = m.group("abi").split(".")
    if any(re.fullmatch(r"cp\d+t", a) for a in abi_tags):
        return False  # free-threaded builds (cp312t) are not this kit's interpreter
    for tag in py_tags:
        if tag in ("py3", "none"):
            return True
        cpython = _cpython(tag)
        if cpython is None:
            continue
        if cpython == python_version:
            return True
        # abi3 wheels are forward-compatible: cp37-abi3 installs on 3.12.
        if cpython < python_version and "abi3" in abi_tags:
            return True
    return False


def _rank(filename: str) -> tuple[int, int]:
    """0 = platform wheel with this interpreter's own ABI, 1 = abi3, 2 = pure python."""
    m = _WHEEL_NAME.match(filename)
    if not m:
        return (3, 3)
    abi = m.group("abi").split(".")[0]
    if m.group("plat") == "any":
        return (2, 0)
    if abi == "abi3":
        return (1, 0)
    return (0, 0)


def _marker_ok(marker: str, platform: str, python_version: tuple[int, int] = PYTHON_VERSION) -> bool:
    """Evaluate the marker subset this lock actually uses.

    Anything outside it counts as "include": a spare wheel costs kilobytes, a missing
    one costs an install.
    """
    if not marker:
        return True
    if marker.strip().startswith(("extra ", "extra==", "extra ==")):
        return False  # optional-dependency edges are never in a kit's closure
    conf = PLATFORMS[platform]
    env = {
        "sys_platform": conf["sys_platform"],
        "os_name": conf["os_name"],
        "platform_machine": conf["platform_machine"],
        "python_version": "%d.%d" % python_version,
        "python_full_version": "%d.%d.0" % python_version,
    }
    for atom in re.split(r"\s+and\s+", marker):
        m = re.fullmatch(
            r"(?P<name>\w+)\s*(?P<op>==|!=|>=|<=|>|<|not in|in)\s*'(?P<val>[^']*)'", atom.strip()
        )
        if not m:
            continue
        name, op, raw = m.group("name"), m.group("op"), m.group("val")
        if name not in env:
            continue
        left, right = env[name], raw
        if op in (">", ">=", "<", "<="):
            lp = [int(x) for x in re.findall(r"\d+", left)]
            rp = [int(x) for x in re.findall(r"\d+", right)]
            width = max(len(lp), len(rp))
            lp += [0] * (width - len(lp))
            rp += [0] * (width - len(rp))
            ok = {">": lp > rp, ">=": lp >= rp, "<": lp < rp, "<=": lp <= rp}[op]
        else:
            ok = {"==": left == right, "!=": left != right, "in": left in right,
                  "not in": left not in right}[op]
        if not ok:
            return False
    return True


def load_packages(text: str) -> dict[str, dict]:
    data = tomllib.loads(text)
    return {p["name"]: p for p in data.get("package", [])}


def closure(packages: dict[str, dict], platform: str, python_version: tuple[int, int] = PYTHON_VERSION) -> list[str]:
    """Transitive runtime dependency set for one platform, extras excluded."""
    root = packages.get(PROJECT_NAME)
    if root is None:
        raise KeyError(f"{PROJECT_NAME} is absent from {LOCK}")
    seen: set[str] = set()
    queue = [d["name"] for d in root.get("dependencies", []) if _marker_ok(d.get("marker", ""), platform, python_version)]
    while queue:
        name = queue.pop()
        if name in seen or name == PROJECT_NAME:
            continue
        pkg = packages.get(name)
        if pkg is None:
            raise KeyError(f"{name} is required but absent from the lock")
        seen.add(name)
        queue.extend(
            d["name"] for d in pkg.get("dependencies", []) if _marker_ok(d.get("marker", ""), platform, python_version)
        )
    return sorted(seen)


def _artifacts(pkg: dict) -> tuple[list[dict], list[dict]]:
    wheels = list(pkg.get("wheels") or [])
    raw_sdist = pkg.get("sdist")
    if isinstance(raw_sdist, dict):
        sdists = [raw_sdist]
    else:
        sdists = list(raw_sdist or [])
    return wheels, sdists


def _entry(name: str, version: str, item: dict, source: str) -> dict:
    url = item["url"]
    return {
        "name": name,
        "version": version,
        "filename": url.rsplit("/", 1)[-1],
        "url": url,
        "sha256": (item.get("hash") or "").replace("sha256:", ""),
        "size": int(item.get("size") or 0),
        "source": source,
    }


def plan(platform: str, python_version: tuple[int, int] = PYTHON_VERSION,
         lock_path: Path = LOCK) -> dict:
    """Plan against a specific lock: the kit's source snapshot comes from a tag, so the
    lock it is built from has to be that tag's, not the working tree's."""
    packages = load_packages(lock_path.read_text(encoding="utf-8"))
    names = closure(packages, platform, python_version)
    artifacts: list[dict] = []
    prebuild: list[dict] = []
    missing: list[str] = []
    if not names:
        # An empty plan would build an empty kit that only fails on a device.
        missing.append("<project closure is empty>")
    for name in names:
        pkg = packages[name]
        wheels, sdists = _artifacts(pkg)
        compatible = [w for w in wheels if wheel_matches(w["url"].rsplit("/", 1)[-1], platform, python_version)]
        if compatible:
            # Exactly one file per package, preferring the build that actually fits the
            # machine: an ABI-specific platform wheel, then an abi3 wheel (forward
            # compatible), then the pure-python one. Carrying every compatible variant
            # doubled the kit for no benefit - uv only ever installs one.
            chosen = sorted(compatible, key=lambda w: (_rank(w["url"].rsplit("/", 1)[-1]), w["url"]))[0]
            artifacts.append(_entry(name, pkg.get("version", ""), chosen, "lock-wheel"))
        elif sdists:
            prebuild.append(_entry(name, pkg.get("version", ""), sdists[0], "prebuild-sdist"))
        else:
            missing.append(name)
    total = sum(a["size"] for a in artifacts) + sum(p["size"] for p in prebuild)
    reached = {a["name"] for a in artifacts} | {p["name"] for p in prebuild}
    return {
        "platform": platform,
        "python_version": ".".join(map(str, python_version)),
        "packages": len(reached),
        "package_names": sorted(reached),
        "wheel_files": len(artifacts),
        "bytes_lock_wheels": sum(a["size"] for a in artifacts),
        "bytes_sdist_to_prebuild": sum(p["size"] for p in prebuild),
        "bytes_total": total,
        "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "artifacts": artifacts,
        "prebuild": prebuild,
        "missing": missing,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    ap.add_argument("--json", dest="out", default="")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--lock", dest="lock", default="",
                    help="uv.lock to plan from (default: the one in this repository)")
    args = ap.parse_args(argv)
    lock_path = Path(args.lock) if args.lock else LOCK
    result = plan(args.platform, lock_path=lock_path)
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    if not args.quiet:
        print(payload if not args.out else
              "%s: %d packages, %d wheels, %d to prebuild, %.1f MB" % (
                  result["platform"], result["packages"], result["wheel_files"],
                  len(result["prebuild"]), result["bytes_total"] / 1048576))
        if result["missing"]:
            print("MISSING ARTIFACTS: " + ", ".join(result["missing"]), file=sys.stderr)
    return 1 if result["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
