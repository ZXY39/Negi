from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / 'prompts'
INCLUDE_RE = re.compile(r'\{\{>\s*([^\s{}]+)\s*\}\}')

def load_prompt(name: str) -> str:
    return _load_prompt(name, stack=())


def _load_prompt(name: str, *, stack: tuple[str, ...]) -> str:
    if name in stack:
        cycle = ' -> '.join((*stack, name))
        raise ValueError(f'Prompt include cycle detected: {cycle}')

    path = (PROMPTS_DIR / name).resolve()
    base_dir = PROMPTS_DIR.resolve()
    if base_dir not in path.parents and path != base_dir:
        raise ValueError(f'Prompt include escapes prompts directory: {name}')

    text = path.read_text(encoding='utf-8')
    next_stack = (*stack, name)

    def _expand(match: re.Match[str]) -> str:
        include_name = match.group(1).strip()
        return _load_prompt(include_name, stack=next_stack)

    return INCLUDE_RE.sub(_expand, text)
