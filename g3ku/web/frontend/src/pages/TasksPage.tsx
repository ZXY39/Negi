import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'

import { createTask, getTaskSummary, getTasks, pauseTask, resumeTask, cancelTask } from '@/lib/api/tasks'
import { PageHeader } from '@/layout/PageHeader'
import { TaskList } from '@/components/tasks/TaskList'
import { TaskToolbar } from '@/components/tasks/TaskToolbar'
import { useLayoutStore } from '@/stores/layoutStore'
import { useTaskUiStore } from '@/stores/taskUiStore'

export function TasksPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const showToast = useLayoutStore((state) => state.showToast)
  const filterScope = useTaskUiStore((state) => state.filterScope)
  const searchText = useTaskUiStore((state) => state.searchText)
  const selectedTaskIds = useTaskUiStore((state) => state.selectedTaskIds)
  const multiSelectMode = useTaskUiStore((state) => state.multiSelectMode)
  const setFilterScope = useTaskUiStore((state) => state.setFilterScope)
  const setSearchText = useTaskUiStore((state) => state.setSearchText)
  const toggleSelectedTaskId = useTaskUiStore((state) => state.toggleSelectedTaskId)
  const setMultiSelectMode = useTaskUiStore((state) => state.setMultiSelectMode)
  const clearTaskSelection = useTaskUiStore((state) => state.clearTaskSelection)
  const [draftTask, setDraftTask] = useState('')

  const summaryQuery = useQuery({ queryKey: ['taskSummary'], queryFn: () => getTaskSummary() })
  const tasksQuery = useQuery({ queryKey: ['tasks', filterScope], queryFn: () => getTasks(filterScope) })

  const createMutation = useMutation({
    mutationFn: () => createTask({ task: draftTask, title: draftTask.slice(0, 48) || '新任务' }),
    onSuccess: async (result) => {
      setDraftTask('')
      showToast('任务已创建', 'success')
      await queryClient.invalidateQueries({ queryKey: ['tasks'] })
      await queryClient.invalidateQueries({ queryKey: ['taskSummary'] })
      navigate(`/tasks/${result.task.task_id}`)
    },
  })

  const batchMutation = useMutation({
    mutationFn: async (action: 'pause' | 'resume' | 'cancel') => {
      for (const taskId of selectedTaskIds) {
        if (action === 'pause') await pauseTask(taskId)
        if (action === 'resume') await resumeTask(taskId)
        if (action === 'cancel') await cancelTask(taskId)
      }
    },
    onSuccess: async () => {
      clearTaskSelection()
      showToast('批量操作已完成', 'success')
      await queryClient.invalidateQueries({ queryKey: ['tasks'] })
      await queryClient.invalidateQueries({ queryKey: ['taskSummary'] })
    },
  })

  const filteredItems = useMemo(() => {
    const items = tasksQuery.data?.items || []
    const needle = searchText.trim().toLowerCase()
    if (!needle) return items
    return items.filter((item) => [item.title, item.task_id, item.brief].join(' ').toLowerCase().includes(needle))
  }, [searchText, tasksQuery.data?.items])

  return (
    <section className="page-section">
      <PageHeader
        title="任务大厅"
        description={summaryQuery.data?.text || '按 task 视角查看主运行时中的全部任务。'}
        actions={
          <div className="header-action-stack">
            <input value={draftTask} onChange={(event) => setDraftTask(event.target.value)} placeholder="快速创建任务：输入用户请求" />
            <button type="button" className="primary-button" disabled={!draftTask.trim() || createMutation.isPending} onClick={() => createMutation.mutate()}>
              新建任务
            </button>
          </div>
        }
      />
      <div className="stats-row">
        <div className="stat-card"><strong>{summaryQuery.data?.total_tasks ?? 0}</strong><span>总任务</span></div>
        <div className="stat-card"><strong>{summaryQuery.data?.in_progress_tasks ?? 0}</strong><span>进行中</span></div>
        <div className="stat-card"><strong>{summaryQuery.data?.failed_tasks ?? 0}</strong><span>失败</span></div>
        <div className="stat-card"><strong>{summaryQuery.data?.unread_tasks ?? 0}</strong><span>未读</span></div>
      </div>
      <TaskToolbar
        scope={filterScope}
        searchText={searchText}
        selectedCount={selectedTaskIds.length}
        multiSelectMode={multiSelectMode}
        onScopeChange={setFilterScope}
        onSearchChange={setSearchText}
        onToggleMultiSelect={() => {
          setMultiSelectMode(!multiSelectMode)
          if (multiSelectMode) clearTaskSelection()
        }}
        onPause={() => batchMutation.mutate('pause')}
        onResume={() => batchMutation.mutate('resume')}
        onCancel={() => batchMutation.mutate('cancel')}
      />
      <TaskList items={filteredItems} selectedTaskIds={selectedTaskIds} multiSelect={multiSelectMode} onOpenTask={(taskId) => navigate(`/tasks/${taskId}`)} onToggleSelect={toggleSelectedTaskId} />
    </section>
  )
}
