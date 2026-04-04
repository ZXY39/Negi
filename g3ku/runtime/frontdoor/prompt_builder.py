from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from g3ku.runtime.project_environment import current_project_environment


_PROMPT_TEMPLATE_VARIABLE = re.compile(r'{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}')


class CeoPromptBuilder:
    _REDUNDANT_PROMPT_FRAGMENTS = (
        '并要求下游执行节点继续评估是否需要派生子节点',
        '优先评估是否应先拆解任务并派生子节点',
    )

    def __init__(self, *, loop) -> None:
        self._loop = loop
        self._repo_prompt_dir = Path(__file__).resolve().parents[1] / 'prompts'

    def build(self, *, skills: list) -> str:
        rendered = self.build_base_prompt()
        visible_skills_block = self.build_visible_skills_block(skills=skills)
        if not visible_skills_block:
            return rendered
        return f'{rendered}\n\n{visible_skills_block}'.strip()

    def build_base_prompt(self) -> str:
        project_environment = current_project_environment(workspace_root=getattr(self._loop, 'workspace', None))
        prompt = self._read_prompt('ceo_frontdoor.md')
        rendered = self._render_prompt(
            prompt,
            {
                'project_python_hint': project_environment.get('project_python_hint') or 'python',
            },
        )
        rendered = self._sanitize_prompt(rendered)
        runtime_environment_block = self._runtime_environment_block(project_environment)
        if runtime_environment_block:
            rendered = f'{rendered}\n\n{runtime_environment_block}'.strip()
        return rendered

    def build_visible_skills_block(self, *, skills: list) -> str:
        return self._visible_skills_block(skills)

    @staticmethod
    def _runtime_environment_block(project_environment: dict[str, Any]) -> str:
        project_python_hint = str(project_environment.get('project_python_hint') or 'python').strip() or 'python'
        return '\n'.join(
            [
                '## Runtime Environment',
                '- When a command must use the exact project interpreter, prefer the runtime-exported environment variables `G3KU_PROJECT_PYTHON`, `G3KU_PROJECT_PYTHON_DIR`, `G3KU_PROJECT_SCRIPTS_DIR`, and `G3KU_PROJECT_PYTHON_HINT`.',
                f'- When the exact interpreter command matters, prefer `{project_python_hint}` instead of assuming plain `python`.',
                '- If the user asks to search, inspect, install, or update a ClawHub skill, first load `clawhub-skill-manager` with `load_skill_context(skill_id="clawhub-skill-manager")` and treat it as the required workflow entry.',
            ]
        ).strip()

    def _read_prompt(self, name: str) -> str:
        path = self._repo_prompt_dir / name
        return path.read_text(encoding='utf-8').strip()

    @staticmethod
    def _visible_skills_block(skills: list[Any]) -> str:
        lines: list[str] = []
        for item in list(skills or []):
            if isinstance(item, dict):
                skill_id = str(item.get('skill_id') or '').strip()
                display_name = str(item.get('display_name') or '').strip()
                description = str(item.get('description') or '').strip()
            else:
                skill_id = str(getattr(item, 'skill_id', '') or '').strip()
                display_name = str(getattr(item, 'display_name', '') or '').strip()
                description = str(getattr(item, 'description', '') or '').strip()
            if not skill_id:
                continue
            label = display_name if display_name and display_name != skill_id else skill_id
            summary = description or display_name or skill_id
            lines.append(
                f'- `{skill_id}` ({label}): {summary}. '
                f'Load with `load_skill_context(skill_id="{skill_id}")` when you need the full workflow.'
            )
        if not lines:
            return ''
        return '\n'.join(
            [
                '## Visible Skills For This Turn',
                '- Only the following skill ids are visible in this turn; do not assume any other skill is available.',
                '- If a workflow requires a skill body, call `load_skill_context` with one of the listed ids only.',
                *lines,
            ]
        )

    @classmethod
    def _sanitize_prompt(cls, prompt: str) -> str:
        sanitized = str(prompt or '')
        for fragment in cls._REDUNDANT_PROMPT_FRAGMENTS:
            sanitized = sanitized.replace(fragment, '')
        return sanitized.strip()

    @staticmethod
    def _render_prompt(prompt: str, context: dict[str, str]) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in context:
                raise KeyError(f'Unknown CEO prompt template variable: {key}')
            return str(context[key])

        return _PROMPT_TEMPLATE_VARIABLE.sub(replace, prompt).strip()
