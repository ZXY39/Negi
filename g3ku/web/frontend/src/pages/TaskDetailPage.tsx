import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'

import { TaskArtifactPanel } from '@/components/tasks/TaskArtifactPanel'
import { TaskNodeDetail } from '@/components/tasks/TaskNodeDetail'
import { TaskTree } from '@/components/tasks/TaskTree'
import { getArtifacts, getTaskDetail } from '@/lib/api/tasks'
import { connectTaskSocket } from '@/lib/ws/taskSocket'
import { PageHeader } from '@/layout/PageHeader'
import type { ApiEnvelope } from '@/lib/types/api'
import type { TaskDetailPayload } from '@/lib/types/task'
import { findNodeRecord } from '@/lib/utils/tree'
import { useLayoutStore } from '@/stores/layoutStore'
import { useTaskUiStore } from '@/stores/taskUiStore'

function expandAll(root: TaskDetailPayload['progress']['root']) {
  const expanded: Record<string, boolean> = {}
  if (!root) return expanded
  const visit = (node: NonNullable<TaskDetailPayload['progress']['root']>) => {
    expanded[node.node_id] = true
    node.children.forEach(visit)
  }
  visit(root)
  return expanded
}

export function TaskDetailPage() {
  const { taskId = '' } = useParams()
  const queryClient = useQueryClient()
  const showToast = useLayoutStore((state) => state.showToast)
  const selectedNodeId = useTaskUiStore((state) => state.selectedNodeId)
  const expandedNodeIds = useTaskUiStore((state) => state.expandedNodeIds)
  const pan = useTaskUiStore((state) => state.pan)
  const selectNode = useTaskUiStore((state) => state.selectNode)
  const toggleNodeExpanded = useTaskUiStore((state) => state.toggleNodeExpanded)
  const setPan = useTaskUiStore((state) => state.setPan)
  const setExpandedNodeIds = useTaskUiStore((state) => state.setExpandedNodeIds)
  const resetTreeUi = useTaskUiStore((state) => state.resetTreeUi)
  const [liveDetail, setLiveDetail] = useState<TaskDetailPayload | null>(null)

  const detailQuery = useQuery({ queryKey: ['taskDetail', taskId], queryFn: () => getTaskDetail(taskId), enabled: !!taskId })
  const artifactsQuery = useQuery({ queryKey: ['artifacts', taskId], queryFn: () => getArtifacts(taskId), enabled: !!taskId })

  useEffect(() => {
    resetTreeUi()
    setLiveDetail(null)
  }, [resetTreeUi, taskId])

  useEffect(() => {
    if (!taskId) return
    const socket = connectTaskSocket({
      taskId,
      onMessage: async (payload: ApiEnvelope) => {
        if (payload.type === 'snapshot.task') {
          setLiveDetail(payload.data as TaskDetailPayload)
        }
        if (payload.type === 'artifact.applied') {
          showToast('补丁工件已应用', 'success')
          await queryClient.invalidateQueries({ queryKey: ['artifacts', taskId] })
          await queryClient.invalidateQueries({ queryKey: ['taskDetail', taskId] })
        }
      },
    })
    return () => socket.close()
  }, [queryClient, showToast, taskId])

  const detail = liveDetail || (detailQuery.data ? { task: detailQuery.data.task, progress: detailQuery.data.progress } : null)

  useEffect(() => {
    const root = detail?.progress.root
    if (!root) return
    setExpandedNodeIds(expandAll(root))
    if (!selectedNodeId) {
      selectNode(root.node_id)
    }
  }, [detail?.progress.root, selectNode, selectedNodeId, setExpandedNodeIds])

  const selectedNodeRecord = useMemo(
    () => findNodeRecord(detail?.progress.nodes || [], selectedNodeId),
    [detail?.progress.nodes, selectedNodeId],
  )

  if (!taskId) {
    return <section className="page-section"><div className="empty-state">缺少 taskId。</div></section>
  }

  if (!detail) {
    return <section className="page-section"><div className="empty-state">正在加载任务详情…</div></section>
  }

  return (
    <section className="page-section task-detail-page">
      <PageHeader title={detail.task.title || detail.task.task_id} description={detail.task.user_request || detail.task.task_id} />
      <div className="task-detail-layout">
        <section className="task-tree-panel">
          <div className="panel-header-row">
            <h3>任务树</h3>
            <span>{detail.progress.nodes.length} 节点</span>
          </div>
          <TaskTree
            root={detail.progress.root}
            selectedNodeId={selectedNodeId}
            expandedNodeIds={expandedNodeIds}
            pan={pan}
            onSelect={selectNode}
            onToggle={toggleNodeExpanded}
            onPanChange={setPan}
          />
        </section>
        <div className="task-detail-side">
          <TaskNodeDetail task={detail.task} node={selectedNodeRecord} />
          <TaskArtifactPanel taskId={taskId} artifacts={artifactsQuery.data?.items || []} />
        </div>
      </div>
    </section>
  )
}
