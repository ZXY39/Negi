from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent / 'prompts'


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / name
    return path.read_text(encoding='utf-8').strip()


@lru_cache(maxsize=32)
def load_prompt_preview(name: str, limit: int = 120) -> str:
    text = ' '.join(load_prompt(name).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + '...'
