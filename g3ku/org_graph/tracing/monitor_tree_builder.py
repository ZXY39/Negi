from __future__ import annotations

from g3ku.org_graph.monitoring.models import MonitorTreeNode, TaskMonitorNodeRecord


class MonitorTreeBuilder:
    def build(self, *, nodes: list[TaskMonitorNodeRecord], root_node_id: str) -> MonitorTreeNode | None:
        if not nodes:
            return None
        node_map = {
            node.node_id: MonitorTreeNode(
                node_id=node.node_id,
                parent_node_id=node.parent_node_id,
                state=node.state,
                wait_reason=node.wait_reason,
                input=node.input,
                output=node.output,
                check=node.check,
                log=list(node.log or []),
                children=[],
                updated_at=node.updated_at,
            )
            for node in sorted(nodes, key=lambda item: (item.created_at, item.node_id))
        }
        for node in sorted(nodes, key=lambda item: (item.created_at, item.node_id)):
            if node.parent_node_id and node.parent_node_id in node_map:
                node_map[node.parent_node_id].children.append(node_map[node.node_id])
        return node_map.get(root_node_id)

    def to_text(self, *, root: MonitorTreeNode | None) -> str:
        if root is None:
            return ''
        lines: list[str] = []

        def walk(node: MonitorTreeNode, prefix: str = '', is_last: bool = True, *, is_root: bool = False) -> None:
            connector = '' if is_root else ('└─ ' if is_last else '├─ ')
            lines.append(f"{prefix}{connector}({node.node_id},{node.state})")
            next_prefix_base = prefix + ('' if is_root else ('   ' if is_last else '│  '))
            for index, child in enumerate(node.children):
                walk(child, next_prefix_base, index == len(node.children) - 1)

        walk(root, is_root=True)
        return '\n'.join(lines)

