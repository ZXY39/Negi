from __future__ import annotations

import hashlib
from pathlib import Path

from g3ku.runtime.multi_agent.dynamic.category_resolver import ResolvedDynamicSpec
from g3ku.runtime.multi_agent.dynamic.types import DynamicSubagentRequest, DynamicSubagentSessionRecord

COMPLETION_PROMISE_TOKEN = '<promise>DONE</promise>'


class DynamicPromptBuilder:
    def __init__(self, *, loop) -> None:
        self._loop = loop
        self._repo_prompt_dir = Path(__file__).resolve().parents[2] / 'prompts'

    def build(
        self,
        *,
        request: DynamicSubagentRequest,
        spec: ResolvedDynamicSpec | None = None,
        profile: ResolvedDynamicSpec | None = None,
        continuation_record: DynamicSubagentSessionRecord | None = None,
    ) -> tuple[str, str, list[str]]:
        resolved_spec = spec or profile or ResolvedDynamicSpec(name='dynamic_worker')
        skill_names = list(dict.fromkeys(resolved_spec.injected_skills))
        skill_sections = self._load_skill_sections(skill_names)
        reference_sections = self._load_reference_sections(skill_names)
        prompt = '\n\n'.join(
            part
            for part in [
                self._read_prompt('dynamic_worker_base.md', self._default_worker_prompt()),
                self._spec_section(resolved_spec),
                skill_sections,
                reference_sections,
                self._tool_policy_section(resolved_spec),
                self._delegation_section(request),
                self._discipline_section(),
                self._continuation_section(continuation_record),
            ]
            if part
        )
        fingerprint = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        return prompt, fingerprint, skill_names

    def build_orchestrator_prompt(self) -> str:
        return self._read_prompt('orchestrator.md', self._default_orchestrator_prompt())

    def _read_prompt(self, name: str, fallback: str) -> str:
        path = self._repo_prompt_dir / name
        if path.exists():
            return path.read_text(encoding='utf-8').strip()
        return fallback

    def _load_skill_sections(self, names: list[str]) -> str:
        registry = getattr(self._loop, 'capability_registry', None)
        loader = getattr(self._loop, 'capability_loader', None)
        if registry is None or loader is None:
            return ''
        blocks: list[str] = []
        for name in names:
            descriptor = registry.resolve_skill(name)
            if descriptor is None:
                continue
            try:
                body = loader.load_skill_body(descriptor).strip()
            except Exception:
                continue
            if body:
                blocks.append(f'## 技能：{name}\n\n{body}')
        return '\n\n'.join(block for block in blocks if block)

    def _load_reference_sections(self, names: list[str]) -> str:
        registry = getattr(self._loop, 'capability_registry', None)
        if registry is None:
            return ''
        blocks: list[str] = []
        for name in names:
            descriptor = registry.resolve_skill(name)
            if descriptor is None:
                continue
            for path in descriptor.reference_paths[:3]:
                try:
                    text = path.read_text(encoding='utf-8').strip()
                except Exception:
                    continue
                if text:
                    blocks.append(f'## 参考：{path.name}\n\n{text[:1800]}')
        return '\n\n'.join(blocks)

    @staticmethod
    def _spec_section(spec: ResolvedDynamicSpec) -> str:
        return (
            '## 运行时规格\n\n'
            f'- role_name: {spec.name}\n'
            f'- role_description: {spec.description or spec.name}\n'
            f'- output_mode: {spec.output_mode}\n'
            f'- mutation_allowed: {str(spec.mutation_allowed).lower()}\n'
        )

    @staticmethod
    def _tool_policy_section(spec: ResolvedDynamicSpec) -> str:
        allowed = ', '.join(spec.tools_allow) if spec.tools_allow else 'none'
        denied = ', '.join(spec.tools_deny) if spec.tools_deny else 'none'
        skills = ', '.join(spec.injected_skills) if spec.injected_skills else 'none'
        return (
            '## 能力边界\n\n'
            f'- allowed_tools: {allowed}\n'
            f'- denied_tools: {denied}\n'
            f'- injected_skills: {skills}\n'
        )

    @staticmethod
    def _delegation_section(request: DynamicSubagentRequest) -> str:
        constraints = '\n'.join(f'- {item}' for item in request.context_constraints) or '- none'
        rules = '\n'.join(f'- {item}' for item in request.action_rules) or '- none'
        schema_text = str(request.output_schema or 'text')
        return (
            '## 委派契约\n\n'
            '### 上下文约束\n'
            f'{constraints}\n\n'
            '### 目标声明\n'
            f'{request.prompt.strip()}\n\n'
            '### 行动规则\n'
            f'{rules}\n\n'
            '### 期望输出\n'
            f'{schema_text}'
        )

    @staticmethod
    def _discipline_section() -> str:
        return (
            '## 运行纪律\n\n'
            '- 你是一个运行时动态创建的临时执行单元，只处理当前这一次委派任务。\n'
            '- 你没有永久专家身份，也不能继续派生额外 agent。\n'
            '- 你只能使用当前注入的工具和技能。\n'
            '- 除非任务明确要求，否则直接给出结果，不要长篇叙述游离过程。\n'
            '- 如果缺少完成任务所需的工具或技能，要简洁说明阻塞原因并停止。\n'
            f'- 当任务完成、阻塞、或已经无法继续产生可验证进展时，结束最终回答时必须附带 {COMPLETION_PROMISE_TOKEN}。\n'
            f'- 如果浏览器、搜索或其它动作出现循环且无进展，给出当前最佳已验证结果、列出阻塞原因，并以 {COMPLETION_PROMISE_TOKEN} 结束。'
        )

    @staticmethod
    def _continuation_section(record: DynamicSubagentSessionRecord | None) -> str:
        if record is None:
            return ''
        return (
            '## 延续锚点\n\n'
            f'- session_id: {record.session_id}\n'
            f'- last_anchor_index: {record.last_anchor_index}\n'
            f"- last_result_summary: {record.last_result_summary or 'none'}\n"
            '请在现有任务状态上继续，不要无故从头重启。只有当新的用户修正明确要求重做时，才重置原计划。'
        )

    @staticmethod
    def _default_worker_prompt() -> str:
        return (
            '你是 Nano 的运行时动态执行单元。\n\n'
            '你是为当前委派临时创建的，不是永久身份。你必须聚焦于当前目标，遵守注入的约束，不能递归派生，也不能越权使用工具。'
            f' 当你完成任务或必须安全停止时，最终回答末尾必须附带 {COMPLETION_PROMISE_TOKEN}。'
        )

    @staticmethod
    def _default_orchestrator_prompt() -> str:
        return (
            '你是 Nano 的主协调者。\n\n'
            '你是唯一长期存在、直接面向用户的 agent。对于简单请求可以直接回答。'
            ' 对于需要持续工具执行、隔离上下文、长时间运行、并发收集、批量处理或可中断恢复的工作，应该调用内部委派工具创建或继续动态子单元。'
            ' 你必须显式给出所需工具、所需技能、上下文约束和行动规则，不要依赖任何预设类别模板。'
            ' 如果已有合适的 session_id，优先继续，而不是重新生成。'
        )

