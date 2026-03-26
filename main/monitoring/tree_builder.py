from __future__ import annotations

from collections.abc import Iterable

from main.models import NodeRecord, TaskRecord
from main.monitoring.models import TaskSpawnRound, TaskTreeNode


class TaskTreeBuilder:
    def build_tree(self, task: TaskRecord, nodes: list[NodeRecord]) -> TaskTreeNode | None:
        if not nodes:
            return None
        ordered = sorted(nodes, key=lambda item: (str(item.created_at or ''), str(item.node_id or '')))
        node_records = {node.node_id: node for node in ordered}
        child_ids_by_parent: dict[str, list[str]] = {}
        for node in ordered:
            if node.parent_node_id and node.parent_node_id in node_records:
                child_ids_by_parent.setdefault(node.parent_node_id, []).append(node.node_id)
        index = {
            node.node_id: TaskTreeNode(
                node_id=node.node_id,
                parent_node_id=node.parent_node_id,
                depth=int(node.depth or 0),
                node_kind=str(node.node_kind or 'execution'),
                status=node.status,
                title=node.goal or node.node_id,
                input=node.input,
                input_ref=str(node.input_ref or ''),
                output='\n'.join(entry.content for entry in node.output if str(entry.content or '').strip()),
                output_ref=next(
                    (
                        str(entry.content_ref or '')
                        for entry in reversed(list(node.output or []))
                        if str(entry.content_ref or '').strip()
                    ),
                    str(node.final_output_ref or ''),
                ),
                check_result=node.check_result,
                check_result_ref=str(node.check_result_ref or ''),
                updated_at=node.updated_at,
                token_usage=node.token_usage,
                token_usage_by_model=list(node.token_usage_by_model or []),
                spawn_rounds=[],
                auxiliary_children=[],
                default_round_id='',
                children=[],
            )
            for node in ordered
        }
        for node in ordered:
            rounds, auxiliary_children = self._build_child_groups(
                parent=node,
                child_ids=child_ids_by_parent.get(node.node_id, []),
                node_records=node_records,
                tree_nodes=index,
            )
            tree_node = index[node.node_id]
            tree_node.spawn_rounds = rounds
            tree_node.auxiliary_children = auxiliary_children
            tree_node.default_round_id = str(rounds[-1].round_id or '') if rounds else ''
            default_round_children = list(rounds[-1].children or []) if rounds else []
            tree_node.children = [*auxiliary_children, *default_round_children]
        return index.get(task.root_node_id)

    def render_tree_text(self, root: TaskTreeNode | None) -> str:
        if root is None:
            return '(empty tree)'
        lines: list[str] = []

        def walk(node: TaskTreeNode, prefix: str = '', *, is_root: bool = False) -> None:
            label = f'({node.node_id},{node.status})'
            lines.append(label if is_root else f'{prefix}|-{label}')
            child_prefix = '' if is_root else f'{prefix}  '
            for child in list(node.children or []):
                walk(child, child_prefix, is_root=False)

        walk(root, is_root=True)
        return '\n'.join(lines)

    def _build_child_groups(
        self,
        *,
        parent: NodeRecord,
        child_ids: Iterable[str],
        node_records: dict[str, NodeRecord],
        tree_nodes: dict[str, TaskTreeNode],
    ) -> tuple[list[TaskSpawnRound], list[TaskTreeNode]]:
        ordered_child_ids = [str(child_id or '').strip() for child_id in list(child_ids or []) if str(child_id or '').strip()]
        if not ordered_child_ids:
            return [], []

        execution_child_ids = [
            child_id
            for child_id in ordered_child_ids
            if str(getattr(node_records.get(child_id), 'node_kind', '') or '').strip().lower() == 'execution'
        ]
        execution_child_set = set(execution_child_ids)
        auxiliary_children = [
            tree_nodes[child_id]
            for child_id in ordered_child_ids
            if child_id in tree_nodes and child_id not in execution_child_set
        ]

        round_specs: list[dict[str, object]] = []
        assigned_execution_ids: set[str] = set()
        spawn_operations = (parent.metadata or {}).get('spawn_operations') if isinstance(parent.metadata, dict) else {}
        if isinstance(spawn_operations, dict):
            for position, (round_id, payload) in enumerate(spawn_operations.items()):
                normalized_round_id = str(round_id or '').strip() or f'explicit:{position + 1}'
                entries = list(payload.get('entries') or []) if isinstance(payload, dict) else []
                round_child_ids: list[str] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    child_id = str(entry.get('child_node_id') or '').strip()
                    if child_id and child_id in execution_child_set and child_id not in round_child_ids:
                        round_child_ids.append(child_id)
                assigned_execution_ids.update(round_child_ids)
                round_specs.append(
                    {
                        'round_id': normalized_round_id,
                        'source': 'explicit',
                        'position': position,
                        'created_at': self._round_created_at(parent=parent, child_ids=round_child_ids, node_records=node_records),
                        'child_ids': round_child_ids,
                        'entries': entries,
                    }
                )

        implicit_child_ids = [child_id for child_id in execution_child_ids if child_id not in assigned_execution_ids]
        if implicit_child_ids:
            round_specs.append(
                {
                    'round_id': self._implicit_round_id(parent=parent, child_ids=implicit_child_ids),
                    'source': 'implicit',
                    'position': len(round_specs),
                    'created_at': self._round_created_at(parent=parent, child_ids=implicit_child_ids, node_records=node_records),
                    'child_ids': implicit_child_ids,
                    'entries': [],
                }
            )

        ordered_round_specs = sorted(
            round_specs,
            key=lambda item: (
                str(item.get('created_at') or ''),
                int(item.get('position') or 0),
                str(item.get('round_id') or ''),
            ),
        )
        rounds: list[TaskSpawnRound] = []
        for index, spec in enumerate(ordered_round_specs, start=1):
            round_child_ids = [child_id for child_id in list(spec.get('child_ids') or []) if child_id in tree_nodes]
            total_children, completed_children, running_children, failed_children = self._round_status_totals(
                child_ids=round_child_ids,
                entries=list(spec.get('entries') or []),
                node_records=node_records,
            )
            rounds.append(
                TaskSpawnRound(
                    round_id=str(spec.get('round_id') or ''),
                    round_index=index,
                    label=f'第{index}轮树',
                    is_latest=False,
                    created_at=str(spec.get('created_at') or ''),
                    child_node_ids=round_child_ids,
                    source=str(spec.get('source') or 'explicit'),
                    total_children=total_children,
                    completed_children=completed_children,
                    running_children=running_children,
                    failed_children=failed_children,
                    children=[tree_nodes[child_id] for child_id in round_child_ids],
                )
            )
        if rounds:
            rounds[-1].is_latest = True
        return rounds, auxiliary_children

    @staticmethod
    def _round_created_at(*, parent: NodeRecord, child_ids: list[str], node_records: dict[str, NodeRecord]) -> str:
        child_times = sorted(str(node_records[child_id].created_at or '') for child_id in child_ids if child_id in node_records)
        if child_times:
            return child_times[0]
        return str(parent.updated_at or parent.created_at or '')

    @staticmethod
    def _implicit_round_id(*, parent: NodeRecord, child_ids: list[str]) -> str:
        if child_ids:
            return f'implicit:{parent.node_id}:{child_ids[0]}'
        return f'implicit:{parent.node_id}'

    @staticmethod
    def _round_status_totals(
        *,
        child_ids: list[str],
        entries: list[object],
        node_records: dict[str, NodeRecord],
    ) -> tuple[int, int, int, int]:
        total_children = max(len(child_ids), len([entry for entry in entries if isinstance(entry, dict)]))
        completed_children = 0
        running_children = 0
        failed_children = 0

        if entries:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                status = str(entry.get('status') or '').strip().lower()
                if status == 'success':
                    completed_children += 1
                elif status == 'error':
                    failed_children += 1
                elif status in {'queued', 'running'}:
                    running_children += 1
            return total_children, completed_children, running_children, failed_children

        for child_id in child_ids:
            record = node_records.get(child_id)
            if record is None:
                continue
            status = str(record.status or '').strip().lower()
            if status == 'success':
                completed_children += 1
            elif status == 'failed':
                failed_children += 1
            else:
                running_children += 1
        return total_children, completed_children, running_children, failed_children
