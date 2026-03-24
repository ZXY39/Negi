from __future__ import annotations

import re
from pathlib import Path

from g3ku.runtime.project_environment import current_project_environment


_PROMPT_TEMPLATE_VARIABLE = re.compile(r'{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}')


class CeoPromptBuilder:
    def __init__(self, *, loop) -> None:
        self._loop = loop
        self._repo_prompt_dir = Path(__file__).resolve().parents[1] / 'prompts'

    def build(self, *, skills: list) -> str:
        project_environment = current_project_environment(workspace_root=getattr(self._loop, 'workspace', None))
        prompt = self._read_prompt('ceo_frontdoor.md')
        return self._render_prompt(
            prompt,
            {
                'project_python_hint': project_environment.get('project_python_hint') or 'python',
                'skill_inventory': self._skill_inventory(skills),
            },
        )

    def _read_prompt(self, name: str) -> str:
        path = self._repo_prompt_dir / name
        return path.read_text(encoding='utf-8').strip()

    @staticmethod
    def _render_prompt(prompt: str, context: dict[str, str]) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in context:
                raise KeyError(f'Unknown CEO prompt template variable: {key}')
            return str(context[key])

        return _PROMPT_TEMPLATE_VARIABLE.sub(replace, prompt).strip()

    @staticmethod
    def _skill_inventory(skills: list) -> str:
        return '\n'.join(
            f'- {skill.skill_id}: {skill.description or skill.display_name}'
            for skill in skills
        )
