import { useEffect, useRef } from 'react'

import { TaskTreeNode } from '@/components/tasks/TaskTreeNode'
import { flattenVisibleTree } from '@/lib/utils/tree'
import type { TaskTreeNode as TaskTreeNodeType } from '@/lib/types/task'

export function TaskTree(props: {
  root: TaskTreeNodeType | null
  selectedNodeId: string | null
  expandedNodeIds: Record<string, boolean>
  pan: { x: number; y: number }
  onSelect: (nodeId: string) => void
  onToggle: (nodeId: string) => void
  onPanChange: (x: number, y: number) => void
}) {
  const dragRef = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null)

  useEffect(() => {
    function handleMove(event: MouseEvent) {
      if (!dragRef.current) return
      const nextX = dragRef.current.panX + event.clientX - dragRef.current.startX
      const nextY = dragRef.current.panY + event.clientY - dragRef.current.startY
      props.onPanChange(nextX, nextY)
    }
    function handleUp() {
      dragRef.current = null
    }
    window.addEventListener('mousemove', handleMove)
    window.addEventListener('mouseup', handleUp)
    return () => {
      window.removeEventListener('mousemove', handleMove)
      window.removeEventListener('mouseup', handleUp)
    }
  }, [props])

  const items = flattenVisibleTree(props.root, props.expandedNodeIds)

  return (
    <div className="task-tree-viewport" onMouseDown={(event) => { if ((event.target as HTMLElement).closest('.tree-node-row')) return; dragRef.current = { startX: event.clientX, startY: event.clientY, panX: props.pan.x, panY: props.pan.y } }}>
      <div className="task-tree-canvas" style={{ transform: `translate(${props.pan.x}px, ${props.pan.y}px)` }}>
        {items.length ? (
          items.map((node) => (
            <TaskTreeNode
              key={node.node_id}
              node={node}
              expanded={props.expandedNodeIds[node.node_id] !== false}
              selected={props.selectedNodeId === node.node_id}
              onSelect={() => props.onSelect(node.node_id)}
              onToggle={() => props.onToggle(node.node_id)}
            />
          ))
        ) : (
          <div className="empty-state">暂无任务树。</div>
        )}
      </div>
    </div>
  )
}
