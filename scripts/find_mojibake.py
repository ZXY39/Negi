from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".worktrees",
}

_BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".tar",
    ".jar",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".wav",
    ".flac",
    ".sqlite",
    ".db",
}

_TEXT_EXTENSIONS = {
    ".py",
    ".pyi",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".scss",
    ".less",
    ".html",
    ".xml",
    ".csv",
    ".tsv",
    ".sql",
    ".sh",
    ".ps1",
    ".cmd",
    ".bat",
    ".lock",
    ".gitattributes",
    ".gitignore",
    ".editorconfig",
}

_DEFAULT_KNOWN_MOJIBAKE_TOKENS = (
    # Common mojibake from UTF-8 bytes mis-decoded as GBK/GB18030.
    "\u7481\u9881\u7d87",  # mojibake marker (expected meaning: 记住)
    "\u6d60\u30e5\u6097",  # mojibake marker (expected meaning: 以后)
    "\u6d60\u5a42\u6097",  # mojibake marker (expected meaning: 今后)
    # Seen in some tool/resource YAMLs historically.
    "\u95f5",  # U+95F5
    "\u95ff",  # U+95FF
    "\u9227",  # U+9227
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    col: int
    severity: str  # "error" | "warn"
    kind: str
    snippet: str
    suggestion: str = ""

    def to_compact_text(self) -> str:
        sug = f" -> {self.suggestion}" if self.suggestion else ""
        return f"{self.path}:{self.line}:{self.col}  {self.severity.upper():5}  {self.kind}  {self.snippet}{sug}"


def _configure_console_encoding() -> None:
    # Avoid UnicodeEncodeError on Windows consoles (cp936/gbk).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass


def _run_git_ls_files(root: Path) -> list[Path]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception:
        return []
    raw = proc.stdout
    parts = [p for p in raw.split(b"\x00") if p]
    out: list[Path] = []
    for item in parts:
        try:
            rel = item.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            rel = item.decode("utf-8", errors="replace")
        out.append((root / rel).resolve())
    return out


def _iter_all_files(root: Path, *, exclude_dirs: set[str]) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for name in filenames:
            yield (Path(dirpath) / name).resolve()


def _is_probably_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:4096]
    # Heuristic: lots of odd control bytes suggests binary.
    odd = 0
    for b in sample:
        if b < 9 or (13 < b < 32):
            odd += 1
    return (odd / max(1, len(sample))) > 0.15


def _is_text_candidate(path: Path, *, data: bytes | None) -> bool:
    suffix = path.suffix.lower()
    if suffix in _BINARY_EXTENSIONS:
        return False
    if suffix in _TEXT_EXTENSIONS:
        return True
    if data is None:
        return False
    return not _is_probably_binary(data)


def _clip(text: str, *, max_len: int = 160) -> str:
    value = str(text or "")
    if len(value) <= max_len:
        return value
    return value[: max(0, max_len - 1)] + "…"


def _first_index_control_char(line: str) -> int | None:
    for idx, ch in enumerate(line):
        code = ord(ch)
        if code < 32 and ch not in ("\t", "\n", "\r"):
            return idx
    return None


def _first_index_pua(line: str) -> int | None:
    for idx, ch in enumerate(line):
        if unicodedata.category(ch) == "Co":
            return idx
    return None


def _try_fix_gb18030_utf8(text: str) -> str | None:
    try:
        return text.encode("gb18030").decode("utf-8")
    except Exception:
        return None


def _try_fix_latin1_utf8(text: str) -> str | None:
    try:
        return text.encode("latin1").decode("utf-8")
    except Exception:
        return None


def _looks_like_improvement(original: str, fixed: str) -> bool:
    if not fixed or fixed == original:
        return False

    # Strong signals that we're in a "wrong decode" situation.
    if "\ufffd" in fixed:
        return False
    if any(unicodedata.category(ch) == "Co" for ch in fixed):
        return False

    # Prefer fixes that introduce common Chinese markers or punctuation.
    common_markers = "的了是不在有我你他她们这那要会可以默认以后今后请记住不要别再"
    common_punct = "，。！？：；（）【】《》“”‘’、"
    orig_score = sum(ch in common_markers for ch in original) + sum(ch in common_punct for ch in original)
    fixed_score = sum(ch in common_markers for ch in fixed) + sum(ch in common_punct for ch in fixed)
    if fixed_score <= orig_score:
        return False

    # Avoid flagging trivial whitespace-only changes.
    return fixed.strip() != original.strip()


def _scan_text(
    *,
    root: Path,
    path: Path,
    text: str,
    known_tokens: tuple[str, ...],
    suggest_fix: bool,
    report_suspect: bool,
) -> list[Finding]:
    rel = str(path.relative_to(root)).replace("\\", "/")
    findings: list[Finding] = []

    token_re: re.Pattern[str] | None = None
    clean_tokens = [t for t in known_tokens if t]
    if clean_tokens:
        token_re = re.compile("|".join(re.escape(t) for t in clean_tokens))

    for line_index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line
        snippet = _clip(line)

        if "\ufffd" in line:
            col0 = line.find("\ufffd")
            findings.append(
                Finding(
                    path=rel,
                    line=line_index,
                    col=col0 + 1,
                    severity="error",
                    kind="REPLACEMENT_CHAR",
                    snippet=snippet,
                )
            )

        ctrl_idx = _first_index_control_char(line)
        if ctrl_idx is not None:
            findings.append(
                Finding(
                    path=rel,
                    line=line_index,
                    col=ctrl_idx + 1,
                    severity="error",
                    kind="CONTROL_CHAR",
                    snippet=snippet,
                )
            )

        pua_idx = _first_index_pua(line)
        if pua_idx is not None:
            findings.append(
                Finding(
                    path=rel,
                    line=line_index,
                    col=pua_idx + 1,
                    severity="error",
                    kind="PRIVATE_USE_CHAR",
                    snippet=snippet,
                )
            )

        if token_re is not None:
            m = token_re.search(line)
            if m is not None:
                findings.append(
                    Finding(
                        path=rel,
                        line=line_index,
                        col=m.start() + 1,
                        severity="error",
                        kind="KNOWN_MOJIBAKE_TOKEN",
                        snippet=snippet,
                    )
                )

        if report_suspect:
            # "Reversible" mojibake candidates, reported as warnings by default.
            fixed = _try_fix_gb18030_utf8(line)
            if fixed is not None and _looks_like_improvement(line, fixed):
                findings.append(
                    Finding(
                        path=rel,
                        line=line_index,
                        col=1,
                        severity="warn",
                        kind="SUSPECT_GB18030_TO_UTF8",
                        snippet=snippet,
                        suggestion=_clip(fixed),
                    )
                )
            fixed = _try_fix_latin1_utf8(line)
            if fixed is not None and _looks_like_improvement(line, fixed):
                findings.append(
                    Finding(
                        path=rel,
                        line=line_index,
                        col=1,
                        severity="warn",
                        kind="SUSPECT_LATIN1_TO_UTF8",
                        snippet=snippet,
                        suggestion=_clip(fixed),
                    )
                )

    if suggest_fix and report_suspect:
        # No-op: per-line suggestions already included where applicable.
        pass

    return findings


def _read_text_file(path: Path) -> tuple[str, bool]:
    data = path.read_bytes()
    try:
        return data.decode("utf-8"), True
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), False


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    parser = argparse.ArgumentParser(description="Scan repo files for mojibake/garbled text.")
    parser.add_argument("--root", default=".", help="Workspace root (defaults to cwd).")
    parser.add_argument(
        "--mode",
        choices=("git", "all"),
        default="git",
        help="File listing mode: git (default) or all (walk filesystem).",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=8_000_000,
        help="Skip files larger than this size (bytes).",
    )
    parser.add_argument(
        "--suggest-fix",
        action="store_true",
        help="Include reversible-decode suggestions for suspect lines (warn-level).",
    )
    parser.add_argument(
        "--no-suspect",
        action="store_true",
        help="Disable reversible-decode heuristics (only high-confidence checks).",
    )
    parser.add_argument(
        "--fail-on-suspect",
        action="store_true",
        help="Return non-zero exit code if suspect warnings are found.",
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="Always return exit code 0 (useful for ad-hoc reporting).",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default="",
        help="Write findings to a JSON file (UTF-8).",
    )
    parser.add_argument(
        "--known-token",
        action="append",
        default=[],
        help="Add an extra known bad token to match (repeatable).",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    mode = str(args.mode).strip().lower()

    if mode == "git":
        paths = _run_git_ls_files(root)
        if not paths:
            # Fallback to filesystem walk when git listing fails.
            paths = list(_iter_all_files(root, exclude_dirs=set(_DEFAULT_EXCLUDE_DIRS)))
    else:
        paths = list(_iter_all_files(root, exclude_dirs=set(_DEFAULT_EXCLUDE_DIRS)))

    known_tokens = tuple([*_DEFAULT_KNOWN_MOJIBAKE_TOKENS, *list(args.known_token or [])])
    report_suspect = (not bool(args.no_suspect))
    suggest_fix = bool(args.suggest_fix)

    all_findings: list[Finding] = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        if st.st_size > int(args.max_bytes):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue

        if not _is_text_candidate(path, data=data):
            continue

        try:
            text, strict_ok = _read_text_file(path)
        except Exception:
            continue

        if not strict_ok:
            # Surface the file-level decode problem via replacement-char findings.
            # (Per-line scan will pinpoint the exact lines with U+FFFD.)
            pass

        all_findings.extend(
            _scan_text(
                root=root,
                path=path,
                text=text,
                known_tokens=known_tokens,
                suggest_fix=suggest_fix,
                report_suspect=report_suspect,
            )
        )

    # De-dupe exact duplicates (same path/line/col/kind).
    unique: list[Finding] = []
    seen: set[tuple[str, int, int, str]] = set()
    for item in all_findings:
        key = (item.path, item.line, item.col, item.kind)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda f: (f.path, f.line, f.col, f.severity, f.kind))

    if args.json_path:
        payload = [
            {
                "path": f.path,
                "line": f.line,
                "col": f.col,
                "severity": f.severity,
                "kind": f.kind,
                "snippet": f.snippet,
                "suggestion": f.suggestion,
            }
            for f in unique
        ]
        Path(args.json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    for item in unique:
        print(item.to_compact_text())

    error_count = sum(1 for f in unique if f.severity == "error")
    warn_count = sum(1 for f in unique if f.severity == "warn")
    print(f"\nFindings: {error_count} error(s), {warn_count} warning(s). Scanned: {len(paths)} file(s).")

    if args.exit_zero:
        return 0
    if error_count > 0:
        return 1
    if args.fail_on_suspect and warn_count > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
