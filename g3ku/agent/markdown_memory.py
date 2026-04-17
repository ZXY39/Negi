from __future__ import annotations

from dataclasses import dataclass
import re

_ENTRY_RE = re.compile(
    r"(?:^|\n)---\n(?P<header>\d{4}/\d{1,2}/\d{1,2}-(?:self|user)：)\n(?P<body>.*?)(?=\n---\n|\Z)",
    re.S,
)
_NOTE_RE = re.compile(r"\bref:(?P<ref>[a-z0-9_]+)\b")


@dataclass(slots=True)
class MemoryEntry:
    date_text: str
    source: str
    summary: str
    note_ref: str = ""


def format_memory_entry(entry: MemoryEntry) -> str:
    return f"---\n{str(entry.date_text).strip()}-{str(entry.source).strip()}：\n{str(entry.summary).strip()}\n"


def parse_memory_document(text: str) -> list[MemoryEntry]:
    normalized = str(text or "").strip()
    if not normalized:
        return []
    entries: list[MemoryEntry] = []
    for match in _ENTRY_RE.finditer(normalized):
        header = str(match.group("header") or "").strip().rstrip("：")
        body = str(match.group("body") or "").strip()
        if not header or not body:
            continue
        date_text, source = header.rsplit("-", 1)
        summary = body.splitlines()[0].strip()
        note_match = _NOTE_RE.search(summary)
        entries.append(
            MemoryEntry(
                date_text=date_text,
                source=source,
                summary=summary,
                note_ref=(note_match.group("ref") if note_match else ""),
            )
        )
    return entries


def validate_memory_document(
    text: str,
    *,
    summary_max_chars: int,
    document_max_chars: int,
) -> None:
    normalized = str(text or "")
    if len(normalized) > int(document_max_chars):
        raise ValueError(f"memory document exceeds {int(document_max_chars)} chars")
    parsed = parse_memory_document(normalized)
    if normalized.strip() and not parsed:
        raise ValueError("memory document contains invalid blocks")
    for item in parsed:
        if len(str(item.summary or "").strip()) > int(summary_max_chars):
            raise ValueError(f"summary line exceeds {int(summary_max_chars)} chars")


def note_file_name(ref: str) -> str:
    normalized = str(ref or "").strip()
    if not normalized:
        raise ValueError("note ref is required")
    return f"{normalized}.md"
