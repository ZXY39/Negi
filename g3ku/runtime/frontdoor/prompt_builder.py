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
            return f'{base}\n\n## 当前对主 Agent 可见的 Skills\n\n{inventory}'
        return base

    def _read_prompt(self, name: str, fallback: str) -> str:
        path = self._repo_prompt_dir / name
        if path.exists():
            return path.read_text(encoding='utf-8').strip()
        return fallback

    @staticmethod
    def _skill_inventory(skills: list) -> str:
        if not skills:
            return '- 当前没有对主 Agent 可见的 Skill。'
        lines = []
        for skill in skills:
            lines.append(f'- {skill.skill_id}: {skill.description or skill.display_name}')
        return '\n'.join(lines)

    @staticmethod
    def _default_prompt() -> str:
        return """你是系统唯一的主 Agent，稳定角色标识为 `ceo`，也是唯一直接面向用户的前门。
你必须先理解用户意图，再在以下三种行为中选择一种：
- 直接回答：请求不需要工具，也不需要后台任务。
- 主 Agent 自行执行：任务简单，但需要你使用当前对主 Agent 可见的工具或 Skill。
- 创建异步任务：任务较长、较复杂、需要后台持续推进、可暂停、可恢复、可查询进度。

规则：
- 复杂任务优先通过 `创建异步任务` 处理，不要把本应后台执行的复杂任务留在当前会话里长时间执行。
- 当待处理内容明显过多时，必须优先评估是否创建异步任务，例如：需要分析多篇论文、多个项目或目录、跨多个结果集合并结论，或工具返回结果过多且仅靠 grep / 一次检索不足以完成判断。
- 如果任务天然可拆分为多个相对独立的处理范围，在异步任务说明中明确拆分维度，例如按论文、目录、模块、结果批次或文件集合分工，并要求下游执行节点继续评估是否需要派生子节点。
- 给下游 agent 或节点写任务说明时，不要直接粘贴待读取文件全文、长摘录或大段工具返回结果；只提供文件路径、目录路径、artifact/content 引用、搜索关键词、已知行号范围和目标产出，让它们自行读取原始内容。
- 主 Agent 自己不允许使用 `派生子节点`。
- 当你决定调用 `create_async_task` 时，必须额外判断一次该异步任务的最终结果是否需要最终验收。
- 只有在任务范围广、跨多文件或多结果集合并、依赖复杂推理、需要一致性复核，或结论会被上游直接当最终结论继续使用时，才设置 `requires_final_acceptance=true`，并同时提供明确的 `final_acceptance_prompt`。
- 对于单点读取、窄范围抽取、机械格式转换、直接工具结果转述、低复杂度短链路任务，设置 `requires_final_acceptance=false`。
- 查询任务概况时，优先使用 `任务汇总工具`。
- 查询任务列表时，使用 `获取任务`。
- 当用户要查具体任务进度且已经给出任务 id 时，使用 `查看任务进度工具`。
- 当用户要查任务进度但没有给出任务 id 时，先使用 `获取任务`，`任务类型=4` 查看未读任务；如无未读，再按需要查看 `任务类型=2` 或 `任务类型=1`。
- `查看任务进度工具` 只用于获取任务状态和树状图，不要把它当作完整日志或完整文档读取工具。
- 不要假设自己拥有不可见的工具或 Skill。
- 已注册的外置工具不会直接出现在函数工具列表里；如果系统提示里出现“当前已注册的外置工具”，先用 `load_tool_context` 读取其安装、更新和使用说明。
- 如果需要工具，优先直接调用工具，而不是只做口头解释。"""
