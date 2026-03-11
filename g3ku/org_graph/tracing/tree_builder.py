from __future__ import annotations

from g3ku.org_graph.models import UnitAgentRecord, UnitTreeNode


class TreeBuilder:
    def build(self, *, units: list[UnitAgentRecord], root_unit_id: str) -> UnitTreeNode:
        nodes = {
            unit.unit_id: UnitTreeNode(
                unit_id=unit.unit_id,
                parent_unit_id=unit.parent_unit_id,
                level=unit.level,
                role_kind=unit.role_kind,
                role_title=unit.role_title,
                objective_summary=unit.objective_summary,
                prompt_preview=unit.prompt_preview,
                status=unit.status,
                current_action=unit.current_action,
                result_summary=unit.result_summary,
                error_summary=unit.error_summary,
                child_count=unit.child_count,
                children=[],
            )
            for unit in units
        }
        for unit in units:
            if unit.parent_unit_id and unit.parent_unit_id in nodes:
                nodes[unit.parent_unit_id].children.append(nodes[unit.unit_id])
        return nodes[root_unit_id]

