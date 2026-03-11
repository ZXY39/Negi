from __future__ import annotations

from pathlib import Path


class CeoPromptBuilder:
    def __init__(self, *, loop) -> None:
        self._loop = loop
        self._repo_prompt_dir = Path(__file__).resolve().parents[1] / 'prompts'

    def build(self, *, skills: list) -> str:
        base = self._read_prompt('ceo_frontdoor.md', self._default_prompt())
        inventory = self._skill_inventory(skills)
        if inventory:
            return f"{base}\n\n## 当前对 CEO 可见的 Skills\n\n{inventory}"
        return base

    def _read_prompt(self, name: str, fallback: str) -> str:
        path = self._repo_prompt_dir / name
        if path.exists():
            return path.read_text(encoding='utf-8').strip()
        return fallback

    @staticmethod
    def _skill_inventory(skills: list) -> str:
        if not skills:
            return '- 当前没有对 CEO 可见的 Skill。'
        lines = []
        for skill in skills:
            lines.append(f"- {skill.skill_id}: {skill.description or skill.display_name}")
        return '\n'.join(lines)

    @staticmethod
    def _default_prompt() -> str:
        return (
            '你是系统唯一的 CEO 主 Agent。\n\n'
            '你始终是用户的私人助手与唯一前门。所有用户请求都必须先由你理解与决策。\n\n'
            '你的三种合法行为只有：\n'
            '1. 直接回答：对不需要工具和后台项目的请求，直接给出答案。\n'
            '2. CEO 自执行：对简单但需要工具的请求，使用当前对 CEO 可见的工具/Skill 直接完成。\n'
            '3. 创建 orgGraph 项目：对长流程、多步骤、需后台持续推进和复盘的任务，创建后台项目。\n\n'
            '规则：\n'
            '- 不要把本应作为后台项目的复杂任务留在当前会话里做。\n'
            '- 不要声称拥有不可见的工具或 Skill。\n'
            '- 只有当前对 CEO 可见的工具和 Skill 才允许使用。\n'
            '- 对任务状态、异常、项目进展等问题，优先使用任务监控工具。'
        )

