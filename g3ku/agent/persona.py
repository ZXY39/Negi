"""The persona carrier: one reserved note, no memory row.

The persona is deliberately not a memory entry. A note carries the body, so it is
outside the resident memory character budget, outside compression and outside
retirement by construction - not by three separate exemptions. The memory agent only
sees a one-line pointer to this note; the CEO frontdoor renders the body in a stable
head block.

Everything that names this note reads the constants below. Do not duplicate the ref
string in callers: the orphan sweep, the note-file collision guard and the frontdoor
notice must agree on the same file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from g3ku.agent.markdown_memory import note_handle

PERSONA_NOTE_REF = "note_persona"
PERSONA_PHOTO_DIR_NAME = "my_image"
PERSONA_PHOTO_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


def persona_note_handle() -> str:
    return note_handle(PERSONA_NOTE_REF)


def persona_digest(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()


def render_persona_block(persona_text: str, *, workspace_root: str | Path) -> str:
    body = str(persona_text or "").strip()
    root = Path(workspace_root).resolve()
    photo_dir = root / PERSONA_PHOTO_DIR_NAME
    who = body if body else "未知（在 Negi中运行）"
    return "\n".join(
        [
            "## 我的人设",
            f"- 我的照片在：项目根目录 {PERSONA_PHOTO_DIR_NAME} 文件夹（{photo_dir}）",
            f"- 我是谁：{who}",
        ]
    )


def render_persona_notice(persona_text: str) -> str:
    heading = "### Persona updates (supersede the identity block at the top)"
    body = str(persona_text or "").strip()
    if not body:
        return "\n".join(
            [
                heading,
                "目前人设不存在，需要向用户询问自己的人设",
            ]
        )
    return "\n".join(
        [
            heading,
            "The identity block at the top is stale. Use this persona from now on:",
            body,
        ]
    )
