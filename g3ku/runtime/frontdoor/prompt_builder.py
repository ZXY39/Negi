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
                '## 运行时环境',
                '- 当命令必须使用项目精确解释器时，优先使用运行时导出的环境变量 `G3KU_PROJECT_PYTHON`、`G3KU_PROJECT_PYTHON_DIR`、`G3KU_PROJECT_SCRIPTS_DIR` 和 `G3KU_PROJECT_PYTHON_HINT`。',
                f'- 当解释器命令必须精确时，优先使用 `{project_python_hint}`，不要默认假设裸 `python` 一定正确。',
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
                l0 = str(item.get('l0') or '').strip()
            else:
                skill_id = str(getattr(item, 'skill_id', '') or '').strip()
                display_name = str(getattr(item, 'display_name', '') or '').strip()
                description = str(getattr(item, 'description', '') or '').strip()
                l0 = str(getattr(item, 'l0', '') or '').strip()
            if not skill_id:
                continue
            label = display_name if display_name and display_name != skill_id else skill_id
            summary = l0 or description or display_name or skill_id
            lines.append(
                f'- `{skill_id}` ({label}): {summary}。'
                f'它不走 hydration；如需读取完整工作流正文，仅在当前已经存在活动阶段后调用 `load_skill_context(skill_id="{skill_id}")`。'
            )
        if not lines:
            return ''
        return '\n'.join(
            [
                '## 本轮可见技能',
                '- 只有以下 `skill_id` 在本轮可见，不要假设其他 skill 可用。',
                '- “可见”不等于“本轮一开始就应该读取正文”；如果当前还没有活动阶段且你需要使用工具，第一步必须先调用 `submit_next_stage`。',
                '- 仅当当前已经存在活动阶段且你确实需要完整工作流正文时，才可对下列 `skill_id` 调用 `load_skill_context`。',
                '- candidate skill 不走 hydration；不要把候选 skill 误当成需要安装或等待下一轮才可读取正文的工具。',
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
