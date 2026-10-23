from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_ENTRY_LINE_RE = re.compile(
    r"^(?P<date>\d{6}):(?P<body>.+?)(?:\s\(note:\s(?P<handle>[A-Za-z0-9_-]{1,80})\))?$"
)


ORDINARY_HANDLE_PREFIX = "n"
CATEGORY_HANDLE_PREFIX = "c"


def note_handle(note_ref: str, *, category: bool = False) -> str:
    """The one token a reader has to pass back to open a note.

    Derived from the file name, so it is stable across renders (the memory block sits at
    message index 1; a changing token would break the prompt cache on every commit) and it
    carries no topic of its own - the description beside it is the only navigation text.
    Ownership stays in the `note_ref` column: nothing may parse this line to decide whether
    a note is still in use.

    The first character names the directory the note lives in: `n` for `memory/notes/`,
    `c` for the category notes in `memory/index/`. Without it two files sharing a stem
    across the two directories would collapse onto one token and every reader would
    silently get whichever one the lookup tried first.
    """
    normalized = str(note_ref or "").strip()
    if not normalized:
        return ""
    prefix = CATEGORY_HANDLE_PREFIX if category else ORDINARY_HANDLE_PREFIX
    return f"{prefix}{hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:5]}"


@dataclass(slots=True)
class MemoryEntry:
    memory_id: str = ""
    date_text: str = ""
    source: str = ""
    summary: str = ""
    note_ref: str = ""


def compact_date(date_text: str) -> str:
    """`2026-10-15` / `2026/10/15` -> `261015`, the only date shape the block prints."""

    parts = [part for part in re.split(r"\D+", str(date_text or "").strip()) if part]
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError("memory date must be a full year/month/day")
    year, month, day = (int(part) for part in parts)
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise ValueError("memory date must be a full year/month/day")
    return f"{year % 100:02d}{month:02d}{day:02d}"


def _expand_date(compact: str) -> str:
    digits = str(compact or "")
    return f"20{digits[:2]}/{int(digits[2:4])}/{int(digits[4:6])}"


def format_memory_entry(entry: MemoryEntry) -> str:
    """Render one memory as exactly one line: `YYMMDD:text (note: handle)`."""

    date_text = str(entry.date_text or "").strip()
    summary = str(entry.summary or "").strip()
    if not summary or "\n" in summary or "\r" in summary:
        raise ValueError("memory summary must be one non-empty line")
    compact = compact_date(date_text)
    note_ref = str(entry.note_ref or "").strip()
    note_tail = f" (note: {note_ref})" if note_ref else ""
    return f"{compact}:{summary}{note_tail}\n"


def parse_memory_document(text: str) -> list[MemoryEntry]:
    """Read the block back line by line.

    A parsed entry carries no memory id on purpose: the block does not print ids, so
    anything that has to name a row addresses it by line number against the row list the
    renderer was built from (`memory_agent_runtime`), never by reconstructing an id here.
    `source` is likewise only a column now.
    """

    normalized = str(text or "").strip()
    if not normalized:
        return []
    entries: list[MemoryEntry] = []
    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _ENTRY_LINE_RE.fullmatch(stripped)
        if not match:
            continue
        body = str(match.group("body") or "").strip()
        if not body:
            continue
        entries.append(
            MemoryEntry(
                date_text=_expand_date(str(match.group("date") or "")),
                summary=body,
                note_ref=str(match.group("handle") or "").strip(),
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
    for item in parsed:
        summary = str(item.summary or "").strip()
        if len(summary) > int(summary_max_chars):
            raise ValueError(f"summary line exceeds {int(summary_max_chars)} chars")


_NOTE_REF_LABEL_RE = re.compile(r"^\(?\s*note\s*:\s*(?P<token>[^)\s]+)\s*\)?$", re.IGNORECASE)


def normalize_note_ref(ref: str) -> str:
    """Accept the shapes a reader copies straight out of the memory block.

    The block prints a handle behind a `note:` label (and inside `(note: ...)` in the
    consolidation index), and a note file is the stem plus `.md`. All of those spellings
    name one target. Only the model-facing reader normalises; the admin URL gate stays
    strict, because there a malformed ref must fail loudly instead of resolving.
    """

    text = str(ref or "").strip()
    match = _NOTE_REF_LABEL_RE.match(text)
    if match:
        text = str(match.group("token") or "").strip()
    if text.lower().endswith(".md"):
        text = text[: -len(".md")].strip()
    return text


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
# 每行说明的唯一上限。这一行是读者决定要不要打开这份 note 之前看到的整句信息，也是这段
# 永久保留的字节里唯一由归并 agent 写的内容；超线的行不进常驻块，由 doctor 与浏览面报出。
CONSOLIDATION_SCOPE_LINE_MAX_CHARS = 500


def consolidation_scope_line(text: str) -> str:
    """The category note's first non-empty line - the one row it gets in the resident block."""

    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")


def render_consolidation_block(rows: list[tuple[str, str]]) -> str:
    """Render the consolidation block from (scope_line, note_ref) pairs, in given order.

    The pair carries the real file name; the line shows its handle, so the description is
    the only topical text a reader sees before deciding to open the note.
    """
    items = [
        (str(scope_line or "").strip(), str(note_ref or "").strip())
        for scope_line, note_ref in list(rows or [])
        if str(scope_line or "").strip() and str(note_ref or "").strip()
    ]
    if not items:
        return ""
    lines = [CONSOLIDATION_HEADING, CONSOLIDATION_REMINDER]
    lines.extend(f"- {scope_line} (note: {note_handle(note_ref, category=True)})" for scope_line, note_ref in items)
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
