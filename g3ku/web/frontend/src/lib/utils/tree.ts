import type { TaskNodeRecord, TaskTreeNode } from '@/lib/types/task'

export function findTreeNode(root: TaskTreeNode | null, nodeId: string | null): TaskTreeNode | null {
  if (!root || !nodeId) return null
  if (root.node_id === nodeId) return root
  for (const child of root.children || []) {
    const found = findTreeNode(child, nodeId)
    if (found) return found
  }
  return null
}

export function collectNodeIds(root: TaskTreeNode | null): string[] {
  if (!root) return []
  return [root.node_id, ...root.children.flatMap((child) => collectNodeIds(child))]
}

export function flattenVisibleTree(root: TaskTreeNode | null, expanded: Record<string, boolean>): TaskTreeNode[] {
  if (!root) return []
  const items: TaskTreeNode[] = [root]
  if (expanded[root.node_id] !== false) {
    for (const child of root.children || []) {
      items.push(...flattenVisibleTree(child, expanded))
    }
  }
  return items
}

export function findNodeRecord(nodes: TaskNodeRecord[], nodeId: string | null) {
  if (!nodeId) return null
  return nodes.find((node) => node.node_id === nodeId) || null
}
