import { ChevronDown, ChevronRight } from 'lucide-react'

import { TaskStatusChip } from '@/components/tasks/TaskStatusChip'
import type { TaskTreeNode as TaskTreeNodeType } from '@/lib/types/task'

export function TaskTreeNode(props: {
  node: TaskTreeNodeType
  expanded: boolean
  selected: boolean
  onToggle: () => void
  onSelect: () => void
}) {
  const hasChildren = (props.node.children || []).length > 0
  return (
    <button type="button" className={`tree-node-row ${props.selected ? 'selected' : ''}`} style={{ marginLeft: `${props.node.depth * 24}px` }} onClick={props.onSelect}>
      <span className="tree-node-leading" onClick={(event) => { event.stopPropagation(); if (hasChildren) props.onToggle() }} role="presentation">
        {hasChildren ? (props.expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />) : <span className="tree-node-dot" />}
      </span>
      <div className="tree-node-content">
        <strong>{props.node.title || props.node.node_id}</strong>
        <span>{props.node.updated_at ? new Date(props.node.updated_at).toLocaleString() : '无更新时间'}</span>
      </div>
      <TaskStatusChip status={props.node.status} />
    </button>
  )
}
