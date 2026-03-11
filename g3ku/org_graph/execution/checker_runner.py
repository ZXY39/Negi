import json
from typing import Any

import json_repair

from g3ku.org_graph.errors import EngineeringFailureError
from g3ku.org_graph.ids import new_unit_id
from g3ku.org_graph.models import UnitAgentRecord, UnitStageRecord
from g3ku.org_graph.prompt_loader import load_prompt, load_prompt_preview
from g3ku.org_graph.protocol import now_iso

CHECKER_SYSTEM_PROMPT = load_prompt('checker.md')


class CheckerRunner:
    def __init__(self, service):
        self._service = service

    async def run_checker_for_item(
        self,
        *,
        project,
        parent_unit: UnitAgentRecord,
        stage: UnitStageRecord,
        candidate_item: dict[str, Any] | None = None,
        item_index: int | None = None,
        acceptance_criteria: str | None = None,
        validation_tools: list[str] | None = None,
        candidate_content: str | None = None,
        source_unit_id: str | None = None,
    ) -> tuple[bool, str]:
        checker_unit = self._build_ephemeral_checker(project=project, parent_unit=parent_unit, stage=stage)
        if candidate_item is None:
            candidate_item = {
                'index': int(item_index or 0),
                'acceptance_criteria': str(acceptance_criteria or ''),
                'validation_tools': list(validation_tools or []),
                'candidate_content': str(candidate_content or ''),
                'source_unit_id': source_unit_id,
            }
        else:
            # Fill legacy fields if the caller supplied both styles.
            if item_index is not None:
                candidate_item = {**candidate_item, 'index': int(item_index)}
            if acceptance_criteria is not None:
                candidate_item = {**candidate_item, 'acceptance_criteria': str(acceptance_criteria)}
            if validation_tools is not None:
                candidate_item = {**candidate_item, 'validation_tools': list(validation_tools)}
            if candidate_content is not None:
                candidate_item = {**candidate_item, 'candidate_content': str(candidate_content)}
            if source_unit_id is not None:
                candidate_item = {**candidate_item, 'source_unit_id': source_unit_id}

        candidate_item = self._normalize_candidate_item(candidate_item)
        
        passed, reason = await self._decide_single_item(
            project=project,
            checker=checker_unit,
            parent_unit=parent_unit,
            stage=stage,
            candidate_item=candidate_item,
        )
        return passed, reason

    def _build_ephemeral_checker(self, *, project, parent_unit: UnitAgentRecord, stage: UnitStageRecord) -> UnitAgentRecord:
        checker_unit_id = new_unit_id('checker')
        checker = UnitAgentRecord(
            unit_id=checker_unit_id,
            project_id=parent_unit.project_id,
            parent_unit_id=parent_unit.unit_id,
            root_unit_id=parent_unit.root_unit_id,
            level=parent_unit.level + 1,
            role_kind='checker',
            role_title=f'检验节点: {stage.title}',
            objective_summary=f'检查阶段 {stage.index} 的候选结果是否满足验收标准',
            prompt_preview=load_prompt_preview('checker.md'),
            status='checking',
            current_stage_id=stage.stage_id,
            can_delegate=False,
            created_at=now_iso(),
            updated_at=now_iso(),
            started_at=now_iso(),
            provider_model=self._service.resolve_project_provider_model(project=project, node_type='inspection'),
            mutation_allowed=False,
        )
        return checker

    async def _decide_single_item(
        self,
        *,
        project,
        checker: UnitAgentRecord,
        parent_unit: UnitAgentRecord,
        stage: UnitStageRecord,
        candidate_item: dict[str, Any],
    ) -> tuple[bool, str]:
        content = await self._service.tool_runtime.run_checker(
            project=project,
            unit=checker,
            parent_unit=parent_unit,
            stage=stage,
            system_prompt=CHECKER_SYSTEM_PROMPT,
            acceptance_criteria=str(candidate_item.get('acceptance_criteria') or ''),
            candidate_content=str(candidate_item.get('candidate_content') or ''),
            validation_tools=list(candidate_item.get('validation_tools') or []),
        )
        try:
            parsed = json_repair.loads(content)
        except Exception as exc:
            raise EngineeringFailureError(f'Checker response is not valid JSON for item {candidate_item["index"]}') from exc
        if not isinstance(parsed, dict):
            raise EngineeringFailureError(f'Checker response must be a JSON object for item {candidate_item["index"]}')

        verdict = str(parsed.get('verdict') or '').lower().strip()
        if verdict not in {'passed', 'failed'}:
            raise EngineeringFailureError(f'Checker verdict is invalid for item {candidate_item["index"]}')
        if verdict == 'failed' and not parsed.get('reason'):
            raise EngineeringFailureError(f'Checker failed verdict must include reason for item {candidate_item["index"]}')

        return verdict == 'passed', str(parsed.get('reason') or '')

    def _normalize_candidate_item(self, item: dict[str, Any]) -> dict[str, Any]:
        tools = item.get('validation_tools') or []
        allowed = set(self._service.list_runtime_tool_names())
        cleaned_tools = [
            str(tool).strip()
            for tool in tools
            if str(tool).strip() and str(tool).strip() in allowed
        ]
        return {
            'index': int(item.get('index') or 0),
            'acceptance_criteria': str(item.get('acceptance_criteria') or ''),
            'validation_tools': cleaned_tools,
            'candidate_content': str(item.get('candidate_content') or ''),
        }



