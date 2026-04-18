from __future__ import annotations

from dataclasses import dataclass
import re

_ENTRY_RE = re.compile(
    r"(?:^|\n)---\n"
    r"id:(?P<memory_id>[A-Za-z0-9]{6})\n"
    r"(?P<header>\d{4}/\d{1,2}/\d{1,2}-(?:self|user)：)\n"
    r"(?P<body>.*?)(?=\n---\n|\Z)",
    re.S,
)
_NOTE_RE = re.compile(r"\bref:(?P<ref>[a-z0-9_]+)\b")
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9]{6}$")
_HEADER_RE = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}-(?:self|user)$")


@dataclass(slots=True)
class MemoryEntry:
    memory_id: str = ""
    date_text: str = ""
    source: str = ""
    summary: str = ""
    note_ref: str = ""


def format_memory_entry(entry: MemoryEntry) -> str:
    memory_id = str(entry.memory_id or "").strip()
    if not _VALID_ID_RE.fullmatch(memory_id):
        raise ValueError("memory id must be exactly 6 alphanumeric characters")
    date_text = str(entry.date_text or "").strip()
    source = str(entry.source or "").strip()
    header = f"{date_text}-{source}"
    if not _HEADER_RE.fullmatch(header):
        raise ValueError("memory entry header must be YYYY/M/D-source")
    summary = str(entry.summary or "").strip()
    if not summary or "\n" in summary or "\r" in summary:
        raise ValueError("memory summary must be one non-empty line")
    return f"---\nid:{memory_id}\n{header}：\n{summary}\n"


def parse_memory_document(text: str) -> list[MemoryEntry]:
    normalized = str(text or "").strip()
    if not normalized:
        return []
    entries: list[MemoryEntry] = []
    for match in _ENTRY_RE.finditer(normalized):
        memory_id = str(match.group("memory_id") or "").strip()
        header = str(match.group("header") or "").strip().rstrip("：")
        body = str(match.group("body") or "").strip()
        if not memory_id or not header or not body:
            continue
        if not _VALID_ID_RE.fullmatch(memory_id):
            continue
        if not _HEADER_RE.fullmatch(header):
            continue
        date_text, source = header.rsplit("-", 1)
        note_match = _NOTE_RE.search(body)
        entries.append(
            MemoryEntry(
                memory_id=memory_id,
                date_text=date_text,
                source=source,
                summary=body,
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
    reconstructed = "".join(format_memory_entry(item) for item in parsed).strip()
    if normalized.strip() != reconstructed:
        raise ValueError("memory document contains invalid blocks")
    seen_ids: set[str] = set()
    for item in parsed:
        summary = str(item.summary or "").strip()
        if len(summary) > int(summary_max_chars):
            raise ValueError(f"summary line exceeds {int(summary_max_chars)} chars")
        if item.memory_id in seen_ids:
            raise ValueError(f"duplicate memory id: {item.memory_id}")
        seen_ids.add(item.memory_id)


def note_file_name(ref: str) -> str:
    normalized = str(ref or "").strip()
    if not normalized:
        raise ValueError("note ref is required")
    return f"{normalized}.md"
