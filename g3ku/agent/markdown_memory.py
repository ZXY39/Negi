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


# 归并区：记忆退场后留在文档里的分类导航。字节由渲染层从库里派生，模型不写这一段的
# 形状；它的存在只为让常驻块里的每条退场记忆还能被找到。措辞一次定稿就不再改：这段
# 注入在 message index 1，改一个字节等于顶掉身后整段历史的 prompt cache。
CONSOLIDATION_HEADING = "## 归并记忆"
CONSOLIDATION_REMINDER = (
    "note 是过去沉淀的经验，需要参考。冲突时以长期记忆为准，其次 note 正文，本节描述只作导航。"
)


def render_consolidation_block(rows: list[tuple[str, str]]) -> str:
    """Render the consolidation block from (scope_line, note_ref) pairs, in given order."""
    items = [
        (str(scope_line or "").strip(), str(note_ref or "").strip())
        for scope_line, note_ref in list(rows or [])
        if str(scope_line or "").strip() and str(note_ref or "").strip()
    ]
    if not items:
        return ""
    lines = [CONSOLIDATION_HEADING, CONSOLIDATION_REMINDER]
    lines.extend(f"- {scope_line} (note: {note_ref})" for scope_line, note_ref in items)
    return "\n".join(lines) + "\n"


def split_consolidation_block(text: str) -> tuple[str, str]:
    """Split a managed document into its memory entries and the trailing block.

    The marker is written by the renderer at a known position, so this is a positional
    read of our own structure - nothing here parses model-authored text.
    """
    normalized = str(text or "")
    marker = f"\n{CONSOLIDATION_HEADING}\n"
    index = normalized.find(marker)
    if index < 0:
        if normalized.startswith(f"{CONSOLIDATION_HEADING}\n"):
            return "", normalized
        return normalized, ""
    return normalized[: index + 1], normalized[index + 1 :]
