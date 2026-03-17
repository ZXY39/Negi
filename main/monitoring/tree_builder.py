from __future__ import annotations

from main.models import NodeRecord, TaskRecord
from main.monitoring.models import TaskTreeNode


class TaskTreeBuilder:
    def build_tree(self, task: TaskRecord, nodes: list[NodeRecord]) -> TaskTreeNode | None:
        if not nodes:
            return None
        ordered = sorted(nodes, key=lambda item: (str(item.created_at or ''), str(item.node_id or '')))
        index = {
            node.node_id: TaskTreeNode(
                node_id=node.node_id,
                parent_node_id=node.parent_node_id,
                depth=int(node.depth or 0),
                status=node.status,
                title=node.goal or node.node_id,
                input=node.input,
                input_ref=str(node.input_ref or ''),
                output='\n'.join(entry.content for entry in node.output if str(entry.content or '').strip()),
                output_ref=next((str(entry.content_ref or '') for entry in reversed(list(node.output or [])) if str(entry.content_ref or '').strip()), str(node.final_output_ref or '')),
                check_result=node.check_result,
                check_result_ref=str(node.check_result_ref or ''),
                updated_at=node.updated_at,
                token_usage=node.token_usage,
                token_usage_by_model=list(node.token_usage_by_model or []),
                children=[],
            )
            for node in ordered
        }
        for node in ordered:
            if node.parent_node_id and node.parent_node_id in index:
                index[node.parent_node_id].children.append(index[node.node_id])
        return index.get(task.root_node_id)

    def render_tree_text(self, root: TaskTreeNode | None) -> str:
        if root is None:
            return '（空树）'
        lines: list[str] = []

        def walk(node: TaskTreeNode, prefix: str = '', *, is_root: bool = False) -> None:
            label = f'（{node.node_id},{node.status}）'
            if is_root:
                lines.append(label)
            else:
                lines.append(f'{prefix}|-{label}')
            child_prefix = '' if is_root else f'{prefix}  '
            for child in list(node.children or []):
                walk(child, child_prefix, is_root=False)

        walk(root, is_root=True)
        return '\n'.join(lines)
