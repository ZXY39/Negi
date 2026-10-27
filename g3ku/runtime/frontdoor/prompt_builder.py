from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from g3ku.runtime.project_environment import (
    current_project_environment,
    format_python_env_reference,
)
from g3ku.runtime.tool_visibility import CEO_FIXED_BUILTIN_TOOL_NAMES
from main.prompts import INCLUDE_RE, load_prompt
from main.runtime.tool_names import (
    FINAL_RESULT_TOOL_NAME,
    SEND_MESSAGE_TOOL_NAME,
    SILENT_TOOL_NAME,
    SPAWN_CHILD_NODES_TOOL_NAME,
    VIEW_MESSAGE_HISTORY_TOOL_NAME,
)

# 提示词只点名前门真会曝光的工具：`stop_tool_execution` / `wait_tool_execution` 没有
# resource.yaml 族、进不了 exposure，`*_v2` 也不是注册名——把它们印进名单等于教模型去发
# 一个发不出的动作。
_CEO_ADVERTISABLE_TOOL_NAMES = frozenset(CEO_FIXED_BUILTIN_TOOL_NAMES) | {
    FINAL_RESULT_TOOL_NAME,
    SEND_MESSAGE_TOOL_NAME,
    SPAWN_CHILD_NODES_TOOL_NAME,
    SILENT_TOOL_NAME,
    VIEW_MESSAGE_HISTORY_TOOL_NAME,
}


def _ceo_prompt_tool_names() -> list[str]:
    return sorted(_CEO_ADVERTISABLE_TOOL_NAMES)


_PROMPT_TEMPLATE_VARIABLE = re.compile(r'{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}')


class CeoPromptBuilder:
    _REDUNDANT_PROMPT_FRAGMENTS = (
        '并要求下游执行节点继续评估是否需要派生子节点',
        '优先评估是否应先拆解任务并派生子节点',
    )

    def __init__(self, *, loop) -> None:
        self._loop = loop
        self._repo_prompt_dir = Path(__file__).resolve().parents[1] / 'prompts'
        self._base_prompt_cache = ''
        self._base_prompt_cache_stamp = ''

    def build(self, *, skills: list) -> str:
        rendered = self.build_base_prompt()
        visible_skills_block = self.build_visible_skills_block(skills=skills)
        if not visible_skills_block:
            return rendered
        return f'{rendered}\n\n{visible_skills_block}'.strip()

    def build_base_prompt(self, *, cache_stamp: str = '', sandbox_tier: str = '') -> str:
        """Render the stable system prompt, re-reading the file only when the stamp moves.

        The stamp is (memory-block adoption time, tool-face exposure revision): the two clocks
        that already decide when the stable prefix changes. Re-reading mid-session would rewrite
        the system bytes while nothing behind them has to move, so a file edit waits for the next
        adoption or exposure change - the same moment the memory block and `tools[]` refresh.

        `sandbox_tier` is deliberately **not** part of the stamp: the declaration below is a
        header, and a header changes only at a reprint boundary. Folding the tier into the stamp
        would reprint the whole prefix the moment an operator flips a session to `jailed`, which
        is exactly the cache miss this contract exists to avoid.
        """

        stamp = str(cache_stamp or '').strip()
        if stamp and self._base_prompt_cache and stamp == self._base_prompt_cache_stamp:
            return self._base_prompt_cache
        project_environment = current_project_environment(workspace_root=getattr(self._loop, 'workspace', None))
        prompt = self._read_prompt('ceo_frontdoor.md')
        rendered = self._render_prompt(
            prompt,
            {'control_tools': ', '.join(_ceo_prompt_tool_names())},
        )
        rendered = self._sanitize_prompt(rendered)
        runtime_environment_block = self._runtime_environment_block(project_environment)
        if runtime_environment_block:
            rendered = f'{rendered}\n\n{runtime_environment_block}'.strip()
        sandbox_block = self._sandbox_declaration_block(sandbox_tier)
        if sandbox_block:
            rendered = f'{rendered}\n\n{sandbox_block}'.strip()
        self._base_prompt_cache = rendered if stamp else ''
        self._base_prompt_cache_stamp = stamp
        return rendered

    @staticmethod
    def _sandbox_declaration_block(tier: str) -> str:
        """What the model is told about the surface it is standing on.

        Without this the jailed model reads host paths, host mounts and `curl` as ordinary
        options and spends a turn per attempt learning otherwise (acceptance item 4 did exactly
        that: `cat /mnt/e/...` then `content_open E:\\...`).
        """
        normalized = str(tier or '').strip()
        if normalized == 'jailed':
            return '\n'.join(
                [
                    '## Sandbox Environment',
                    '- 每条命令都在一次性 bwrap jail 里执行：只有 `/workspace` 与 `/shared` 会落盘，'
                    '进程结束即消失，没有后台进程可留。',
                    '- 宿主路径在 jail 里不存在：`E:\\…`、`/mnt/…`、`/root`、数据根都读不到也写不到，'
                    '`ls /` 只有系统根与上面两个卷。',
                    '- 网络被关闭（`network=none`）：`curl` / `wget` / `getent` 一律失败，'
                    '外部内容只能通过已有上下文或工具回补。',
                    '- 需要产出文件就写 `/workspace`；写完可以用 `filesystem_stat` / `content_open` 自检。',
                ]
            )
        if normalized == 'readonly':
            return '\n'.join(
                [
                    '## Read-only Session',
                    '- 本会话是只读档：写类工具（filesystem_* / edit / propose_patch）被拒，'
                    '`exec` 对第三方会话整条拒绝，owner 会话降为 governed。',
                    '- 可读范围限定在会话上传目录与临时目录（owner 另含工作区），'
                    '读取按真实路径判，软链指到范围外同样被拒。',
                ]
            )
        return ''

    def build_visible_skills_block(self, *, skills: list) -> str:
        return self._visible_skills_block(skills)

    @staticmethod
    def _runtime_environment_block(project_environment: dict[str, Any]) -> str:
        env_reference = format_python_env_reference(project_environment.get('shell_family'))
        return '\n'.join(
            [
                '## 运行时环境',
                '- 当命令必须使用项目精确解释器时，优先使用运行时导出的环境变量 `PROJECT_PYTHON`、`PROJECT_PYTHON_DIR`、`PROJECT_SCRIPTS_DIR` 和 `PROJECT_PYTHON_HINT`。',
                f'- 当解释器命令必须精确时，优先使用 `{env_reference}`，不要默认假设裸 `python` 一定正确。',
            ]
        ).strip()

    def _read_prompt(self, name: str) -> str:
        # `{{> x.md}}` 走节点道同一个展开器：五条全局工具规则只在
        # `main/prompts/shared_tool_reuse_rules.md` 存在一份，两条车道读到的字节必然相同。
        # 抄第二份的话，改一条规则要记得改两处，而第二处没有任何东西会替 reminders 报警。
        path = self._repo_prompt_dir / name
        text = path.read_text(encoding='utf-8').strip()
        return INCLUDE_RE.sub(lambda match: load_prompt(match.group(1).strip()), text).strip()

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
            lines.append(f'- `{skill_id}` ({label}): {summary}。')
        if not lines:
            return ''
        return '\n'.join(
            [
                '## 本轮可见技能',
                '- 只有以下 `skill_id` 在本轮可见，不要假设其他 skill 可用。',
                '- “可见”不等于“本轮一开始就应该读取正文”；需要完整工作流正文时，对下列 `skill_id` 调用 `load_skill_context(skill_id="<skill_id>")`。',
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
